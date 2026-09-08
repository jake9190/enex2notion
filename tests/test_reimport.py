import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from enex2notion.cli_args import parse_args
from enex2notion.cli_reimport import Reimporter, canonical, preview_blocks, reimport_command, state_directory
from enex2notion.cli_upload import EnexUploader
from enex2notion.enex_types import EvernoteNote
from enex2notion.enex_types import EvernoteResource
from enex2notion.enex_uploader import _collect_uploadable_blocks
from enex2notion.import_state import ImportRemote, edit_signature, latest_edit
from enex2notion.notion_blocks.text import NotionTextBlock, TextProp
from enex2notion.notion_blocks.uploadable import NotionAudioBlock, NotionImageBlock, NotionVideoBlock
from enex2notion.utils_static import Rules
from enex2notion.reimport_attachments import hosted_digest


def test_reimport_options():
    args = parse_args([
        "--token", "test-token", "--reimport", "--whatif", "5",
        "--skip-edited-after", "2026-09-01T10:00:00Z",
        "--skip-changed-since-import", "notes.enex",
    ])
    assert args.whatif == 5
    assert args.skip_edited_after == datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
    assert args.skip_changed_since_import


@pytest.mark.parametrize("options", [
    ["--whatif", "2"],
    ["--reimport", "--whatif", "0"],
    ["--reimport", "--whatif", "-1"],
    ["--reimport", "--skip-edited-after", "2026-09-01"],
    ["--reimport", "--skip-edited-after", "bad-date"],
    ["--skip-changed-since-import"],
    ["--reimport", "--resolve-links"],
    ["--reimport", "--dry-run"],
])
def test_invalid_reimport_options(options):
    with pytest.raises(SystemExit):
        parse_args(["--token", "test-token", *options, "notes.enex"])


def test_nested_edits_are_detected():
    snapshot = {
        "page": {"id": "page", "last_edited_time": "2026-09-01T00:00:00Z"},
        "blocks": [{"id": "parent", "last_edited_time": "2026-09-01T00:00:00Z", "_children": [
            {"id": "child", "last_edited_time": "2026-09-02T00:00:00Z"},
        ]}],
    }
    before = edit_signature(snapshot)
    snapshot["blocks"][0]["_children"][0]["last_edited_time"] = "2026-09-03T00:00:00Z"
    assert edit_signature(snapshot) != before
    assert latest_edit(snapshot) == datetime(2026, 9, 3, tzinfo=timezone.utc)


def test_read_pagination(mocker):
    wrapper = mocker.Mock()
    wrapper._rate_limit_delay = 0
    wrapper._retry_on_rate_limit.side_effect = lambda function, **kwargs: function(**kwargs)
    operation = mocker.Mock(side_effect=[
        {"results": [1], "has_more": True, "next_cursor": "next"},
        {"results": [2], "has_more": False},
    ])
    assert ImportRemote(wrapper).paginate(operation, page_size=100) == [1, 2]
    assert operation.call_args.kwargs["start_cursor"] == "next"


def make_note(title="Note", content="<div>Replacement</div>"):
    return EvernoteNote(
        title=title, created=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated=datetime(2026, 1, 2, tzinfo=timezone.utc),
        content=f"<en-note>{content}</en-note>", tags=[], author="", url="",
        is_webclip=False, is_email=False, resources=[],
    )


def make_snapshot(text="Original", edited="2026-09-01T00:00:00Z"):
    return {
        "page": {
            "id": "page", "last_edited_time": edited, "parent": {"page_id": "notebook"},
            "properties": {"title": {"type": "title", "title": [
                {"type": "text", "plain_text": "Note", "text": {"content": "Note"}},
            ]}},
        },
        "blocks": [{
            "id": "old", "last_edited_time": edited, "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]},
        }],
    }


@pytest.fixture()
def importer(tmp_path, mocker):
    args = parse_args(["--token", "test-token", "--reimport", "--mode", "PAGE", "notes.enex"])
    instance = Reimporter(mocker.Mock(), args, tmp_path, output=mocker.Mock())
    instance.remote = mocker.Mock()
    instance.remote.snapshot.return_value = make_snapshot()
    return instance


def make_plan(importer, note=None):
    return importer.plan(note or make_note(), "notebook", [make_snapshot()["page"]], None)


@pytest.mark.parametrize("page_count", [0, 2])
def test_missing_or_ambiguous_matches_are_skipped(importer, page_count):
    plan = importer.plan(make_note(), "notebook", [make_snapshot()["page"]] * page_count, None)
    assert plan["action"] == "skip"
    importer.remote.snapshot.assert_not_called()
    assert importer.wrapper.mock_calls == []


def test_matching_is_scoped_to_notebook(importer):
    page = make_snapshot()["page"]
    page["parent"] = {"page_id": "other"}
    assert importer.plan(make_note(), "notebook", [page], None)["action"] == "skip"


@pytest.mark.parametrize("cutoff,expected", [
    ("2026-08-31T23:59:59+00:00", "skip"),
    ("2026-09-01T00:00:00+00:00", "replace"),
    ("2026-08-31T20:00:00-04:00", "replace"),
])
def test_cutoff_is_strict_and_timezone_aware(importer, cutoff, expected):
    importer.args.skip_edited_after = datetime.fromisoformat(cutoff)
    assert make_plan(importer)["action"] == expected
    assert importer.wrapper.mock_calls == []


def test_since_import_skips_unknown_baseline(importer):
    importer.args.skip_changed_since_import = True
    plan = make_plan(importer)
    assert plan["action"] == "skip"
    assert "no recorded import baseline" in plan["reason"]


def test_since_import_detects_changes_and_uses_recorded_id(importer):
    note = make_note()
    importer.state.record("notebook", note, make_snapshot())
    importer.args.skip_changed_since_import = True
    page = make_snapshot()["page"]
    assert importer.plan(note, "notebook", [page, {**page, "id": "duplicate"}], None)["action"] == "replace"
    importer.remote.snapshot.return_value = make_snapshot(edited="2026-09-02T00:00:00Z")
    assert make_plan(importer)["action"] == "skip"


def test_unchanged_content_is_not_replaced(importer):
    importer.remote.snapshot.return_value = make_snapshot(text="Replacement")
    assert make_plan(importer)["action"] == "unchanged"


def test_preview_has_no_writes(importer, tmp_path):
    importer.args.whatif = 2
    plan = make_plan(importer)
    importer.display("Note", plan)
    assert plan["action"] == "replace"
    rendered = "\n".join(call.args[0] for call in importer.output.call_args_list)
    assert "Original" in rendered and "Replacement" in rendered
    assert list(tmp_path.iterdir()) == []
    assert importer.wrapper.mock_calls == []
    with pytest.raises(RuntimeError, match="Preview cannot"):
        importer.execute(plan)


def test_canonical_attachment_reference_keeps_identity():
    assert canonical({"file_upload": {"id": "ENEX:hash:file.png"}}) == {
        "file_upload": {"id": "ENEX:hash:file.png"},
    }


@pytest.mark.parametrize("heading_type", ["heading_1", "heading_2", "heading_3"])
def test_default_heading_toggle_compares_equal(heading_type):
    proposed = {"type": heading_type, heading_type: {"rich_text": [
        {"type": "text", "text": {"content": "Heading"}},
    ]}}
    existing = copy.deepcopy(proposed)
    existing[heading_type]["is_toggleable"] = False
    assert canonical(existing) == canonical(proposed)
    existing[heading_type]["is_toggleable"] = True
    assert canonical(existing) != canonical(proposed)


def test_empty_paragraph_representations_compare_equal():
    empty = {"type": "paragraph", "paragraph": {"rich_text": []}}
    explicit = {"type": "paragraph", "paragraph": {"rich_text": [
        {"type": "text", "text": {"content": ""}},
    ]}}
    assert canonical(empty) == canonical(explicit)
    assert canonical([empty, explicit]) != canonical([empty])
    for content in (" ", "\n", "text"):
        nonempty = copy.deepcopy(explicit)
        nonempty["paragraph"]["rich_text"][0]["text"]["content"] = content
        assert canonical(empty) != canonical(nonempty)
    with_children = copy.deepcopy(explicit)
    with_children["paragraph"]["children"] = [empty]
    assert canonical(empty) != canonical(with_children)


@pytest.mark.parametrize("level", [1, 2, 3])
def test_default_heading_toggle_does_not_replace_heading(importer, level):
    snapshot = make_snapshot(text="Heading")
    heading = snapshot["blocks"][0]
    heading["type"] = f"heading_{level}"
    heading[heading["type"]] = heading.pop("paragraph")
    heading[heading["type"]]["is_toggleable"] = False
    importer.remote.snapshot.return_value = snapshot
    plan = make_plan(importer, make_note(content=f"<h{level}>Heading</h{level}>"))
    assert plan["action"] == "unchanged"
    assert plan["retained"][0]["id"] == "old"


def test_empty_paragraph_is_retained_during_reimport(importer):
    snapshot = make_snapshot()
    snapshot["blocks"][0]["paragraph"]["rich_text"] = []
    importer.remote.snapshot.return_value = snapshot
    plan = make_plan(importer, make_note(content="<div><br/></div>"))
    assert plan["action"] == "unchanged"
    assert plan["retained"][0]["id"] == "old"
    importer.wrapper.append_blocks.assert_not_called()


def test_empty_paragraph_with_date_changes_only_updates_metadata(importer, tmp_path):
    note = make_note(content="<div><br/></div>")
    snapshot = make_snapshot()
    snapshot["blocks"][0]["paragraph"]["rich_text"] = []
    snapshot["page"]["properties"].update({
        "Created": {"date": {"start": note.created.isoformat()}},
        "Updated": {"date": {"start": note.updated.isoformat()}},
    })
    note.created = note.created.replace(second=27)
    note.updated = note.updated.replace(second=47)
    importer.remote.snapshot.return_value = snapshot
    schema = {"title": {"title": {}}, "Created": {"date": {}}, "Updated": {"date": {}}}
    plan = importer.plan(note, "notebook", [snapshot["page"]], schema)
    assert plan["action"] == "replace"
    assert plan["retained"][0]["id"] == "old"
    assert set(plan["properties"]) == {"Created", "Updated"}
    importer.execute(plan)
    importer.wrapper.append_blocks.assert_not_called()
    importer.remote.archive.assert_not_called()
    importer.remote.update_properties.assert_called_once_with("page", plan["properties"])
    journal = json.loads(next((tmp_path / "reimport-backups").glob("*.json")).read_text())
    assert journal["status"] == "complete"
    assert journal["new_root_ids"] == journal["archived_ids"] == []


def test_equivalent_url_and_date_representations_compare_equal():
    before = {
        "URL": {"url": "https://example.com/path#:~:text=dogs%20who%27ve"},
        "Created": {"date": {"start": "2026-02-26T21:32:00.000+00:00", "end": None, "time_zone": None}},
    }
    after = {
        "URL": {"url": "https://example.com/path#:~:text=dogs%20who've"},
        "Created": {"date": {"start": "2026-02-26T16:32:00-05:00"}},
    }
    assert canonical(before) == canonical(after)


def test_real_date_and_reserved_url_differences_are_preserved():
    assert canonical({"date": {"start": "2026-02-26T21:32:00Z"}}) != canonical(
        {"date": {"start": "2026-02-26T21:32:10Z"}}
    )
    assert canonical({"url": "https://example.com/a%2Fb?x=a%26b"}) != canonical(
        {"url": "https://example.com/a/b?x=a&b"}
    )


def configure_success(importer, mocker):
    original = make_snapshot()
    completed = make_snapshot(text="Replacement", edited="2026-09-02T00:00:00Z")
    completed["blocks"][0]["id"] = "new"
    staged = copy.deepcopy(original)
    staged["blocks"].extend(completed["blocks"])
    staged["page"]["last_edited_time"] = completed["page"]["last_edited_time"]
    importer.remote.snapshot.side_effect = [original, original, staged, completed]
    mocker.patch("enex2notion.cli_reimport._process_image_blocks", return_value=[])
    importer.wrapper.append_blocks.return_value = [{"id": "new"}]
    return original, staged, completed


def test_replacement_keeps_page_id_and_archives_only_after_upload(importer, mocker, tmp_path):
    plan = make_plan(importer)
    configure_success(importer, mocker)

    def archive(block_id):
        importer.wrapper.append_blocks.assert_called_once()
        assert block_id == "old"

    importer.remote.archive.side_effect = archive
    importer.execute(plan)
    assert importer.wrapper.append_blocks.call_args.kwargs["block_id"] == "page"
    importer.wrapper.create_page.assert_not_called()
    importer.remote.update_properties.assert_not_called()
    record = importer.state.get("notebook", make_note())
    assert record["page_id"] == "page"
    journal = json.loads(next((tmp_path / "reimport-backups").glob("*.json")).read_text())
    assert journal["status"] == "complete"
    assert journal["original"]["blocks"][0]["id"] == "old"


def test_reimport_preserves_promoted_children_and_preview_counts(importer, mocker, parse_html):
    from enex2notion.note_parser.blocks import parse_note_blocks

    parent = parse_note_blocks(parse_html("<h1>Heading</h1>"))[0]
    parent.children.append(NotionTextBlock(text_prop=TextProp("Child")))
    mocker.patch("enex2notion.cli_reimport.parse_note", return_value=([parent], []))
    plan = make_plan(importer)
    assert [block["type"] for block in plan["proposed"]] == ["heading_1", "paragraph"]
    original = make_snapshot()
    added = [
        {**copy.deepcopy(block), "id": f"new-{index}", "last_edited_time": "2026-09-01T00:00:00Z"}
        for index, block in enumerate(plan["proposed"])
    ]
    staged = copy.deepcopy(original)
    staged["blocks"].extend(added)
    completed = copy.deepcopy(original)
    completed["blocks"] = added
    importer.remote.snapshot.side_effect = [original, original, staged, completed]
    importer.wrapper.append_blocks.return_value = added

    importer.execute(plan)

    importer.wrapper.append_blocks.assert_called_once()
    assert importer.wrapper.append_blocks.call_args.kwargs["children"] == plan["proposed"]
    importer.remote.archive.assert_called_once_with("old")


def test_upload_failure_leaves_original_blocks_and_no_baseline(importer, mocker, tmp_path):
    plan = make_plan(importer)
    configure_success(importer, mocker)
    importer.wrapper.append_blocks.side_effect = RuntimeError("invalid block")
    with pytest.raises(RuntimeError, match="recovery record"):
        importer.execute(plan)
    importer.remote.archive.assert_not_called()
    importer.remote.update_properties.assert_not_called()
    assert not (tmp_path / "import-state.json").exists()
    importer.remote.snapshot.side_effect = None
    assert "unresolved recovery" in make_plan(importer)["reason"]


def test_edit_between_plan_and_update_prevents_writes(importer, tmp_path):
    plan = make_plan(importer)
    importer.remote.snapshot.return_value = make_snapshot(edited="2026-09-02T00:00:00Z")
    with pytest.raises(RuntimeError, match="changed after planning"):
        importer.execute(plan)
    assert importer.wrapper.mock_calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("returned_ids", [[], ["new", "extra"]])
def test_unexpected_upload_count_records_response_and_preserves_original(importer, mocker, tmp_path, returned_ids):
    plan = make_plan(importer)
    configure_success(importer, mocker)
    importer.wrapper.append_blocks.return_value = [{"id": block_id} for block_id in returned_ids]
    with pytest.raises(RuntimeError, match=f"expected 1, received {len(returned_ids)}"):
        importer.execute(plan)
    importer.wrapper.append_blocks.assert_called_once()
    importer.remote.archive.assert_not_called()
    importer.remote.update_properties.assert_not_called()
    journal = json.loads(next((tmp_path / "reimport-backups").glob("*.json")).read_text())
    assert journal["status"] == "incomplete"
    assert journal["new_root_ids"] == returned_ids
    assert journal["archived_ids"] == []
    assert not (tmp_path / "import-state.json").exists()


def test_edit_during_upload_prevents_archiving(importer, mocker):
    plan = make_plan(importer)
    original, staged, _ = configure_success(importer, mocker)
    staged["blocks"][0]["last_edited_time"] = "2026-09-03T00:00:00Z"
    importer.remote.snapshot.side_effect = [original, original, staged]
    with pytest.raises(RuntimeError, match="Original content changed"):
        importer.execute(plan)
    importer.remote.archive.assert_not_called()
    importer.remote.update_properties.assert_not_called()


@pytest.mark.parametrize("returned_ids", [["old"], ["old", "new"]])
def test_recovery_never_labels_original_blocks_as_new(importer, mocker, tmp_path, returned_ids):
    plan = make_plan(importer)
    configure_success(importer, mocker)
    importer.wrapper.append_blocks.return_value = [{"id": block_id} for block_id in returned_ids]
    with pytest.raises(RuntimeError, match="recovery record"):
        importer.execute(plan)
    importer.remote.archive.assert_not_called()
    importer.remote.update_properties.assert_not_called()
    journal = json.loads(next((tmp_path / "reimport-backups").glob("*.json")).read_text())
    assert journal["status"] == "incomplete"
    assert journal["new_root_ids"] == [block_id for block_id in returned_ids if block_id != "old"]


def test_database_properties_leave_custom_fields_alone(importer):
    schema = {
        "Name": {"type": "title"}, "Tags": {"type": "multi_select"},
        "URL": {"type": "url"}, "Personal URL": {"type": "url"},
        "Workflow": {"type": "multi_select"}, "Review date": {"type": "date"},
    }
    plan = importer.plan(make_note(), "notebook", [make_snapshot()["page"]], schema)
    assert set(plan["properties"]) == {"Name"}


def test_command_whatif_limit_is_global_and_read_only(tmp_path, mocker, capsys):
    args = parse_args([
        "--token", "test-token", "--reimport", "--whatif", "1", "--mode", "PAGE",
        "--operations-dir", str(tmp_path / "not-created"), "first.enex", "second.enex",
    ])
    wrapper = mocker.Mock()
    mocker.patch("enex2notion.cli_reimport.get_notion_wrapper", return_value=wrapper)
    remote = mocker.Mock()
    mocker.patch("enex2notion.cli_reimport.ImportRemote", return_value=remote)
    remote.search.side_effect = [
        [{"id": "root", "object": "page"}],
        [{"id": "notebook", "object": "page", "parent": {"page_id": "root"}}],
    ]
    remote.notebook_pages.return_value = [make_snapshot()["page"]]
    remote.snapshot.return_value = make_snapshot()
    parse = mocker.patch("enex2notion.cli_reimport.parse_all_notes", return_value=SimpleNamespace(results=[
        SimpleNamespace(note=make_note(), failed=False),
        SimpleNamespace(note=make_note(title="Second"), failed=False),
    ]))
    reimport_command(args)
    assert "WHATIF: 1 notes examined" in capsys.readouterr().out
    parse.assert_called_once()
    assert wrapper.mock_calls == []
    remote.archive.assert_not_called()
    remote.update_properties.assert_not_called()
    assert not (tmp_path / "not-created").exists()


@pytest.mark.parametrize("block_class", [NotionImageBlock, NotionAudioBlock, NotionVideoBlock])
def test_attachment_preview_does_not_upload_or_mutate(block_class):
    resource = EvernoteResource(b"content", 7, "hash", "application/octet-stream", "attachment.bin")
    block = block_class(md5_hash="hash", resource=resource)
    payload = preview_blocks([block])
    assert payload[0][payload[0]["type"]]["file_upload"]["id"] == "ENEX:hash:attachment.bin"
    assert "file_upload_id" not in block.attrs
    found = []
    _collect_uploadable_blocks([block], found)
    assert found == [block]


def test_ordinary_successful_import_records_baseline(tmp_path, mocker):
    wrapper = mocker.Mock()
    mocker.patch("enex2notion.cli_upload.ExceptionTracker")
    remote = mocker.patch("enex2notion.cli_upload.ImportRemote").return_value
    remote.snapshot.return_value = make_snapshot()
    uploader = EnexUploader(wrapper, "root", "PAGE", None, Rules(False, False, False, None), working_dir=tmp_path)
    uploader.notebook_root = "notebook"
    mocker.patch.object(uploader, "_parse_note", return_value=([NotionTextBlock(text_prop=TextProp("text"))], []))
    mocker.patch.object(uploader, "_upload_note", return_value=("page", False, [], [], {}))
    assert uploader._upload_single_note(make_note(), 1, 1, "notebook", None) == ("success", None)
    assert uploader.import_state.get("notebook", make_note())["signature"] == edit_signature(make_snapshot())
    assert (tmp_path / "import-state.json").exists()


def test_database_query_is_paginated_and_version_pinned(mocker):
    wrapper = mocker.Mock()
    wrapper._rate_limit_delay = 0
    wrapper._auth_token = "test-token"
    wrapper._retry_on_rate_limit.side_effect = lambda function, **kwargs: function(**kwargs)
    first, second = mocker.Mock(), mocker.Mock()
    first.json.return_value = {"results": [{"id": "first"}], "has_more": True, "next_cursor": "cursor"}
    second.json.return_value = {"results": [{"id": "second"}], "has_more": False}
    post = mocker.patch("enex2notion.import_state.requests.post", side_effect=[first, second])
    assert ImportRemote(wrapper).notebook_pages("notebook", "DB") == [{"id": "first"}, {"id": "second"}]
    assert post.call_args.kwargs["headers"]["Notion-Version"] == "2022-06-28"
    assert post.call_args.kwargs["json"]["start_cursor"] == "cursor"


def test_preview_cli_does_not_open_log_file(mocker):
    from enex2notion.cli import cli

    logging = mocker.patch("enex2notion.cli.setup_logging")
    for name in ("validate_python_version", "validate_requirements", "check_optional_tools", "_upload_cli"):
        mocker.patch(f"enex2notion.cli.{name}")
    cli(["--token", "test-token", "--reimport", "--whatif", "1", "--log", "not-written.log", "notes.enex"])
    logging.assert_called_once_with(False, None)


def test_state_directory_matches_regular_import_priority(tmp_path):
    args = parse_args(["--token", "test-token", "--reimport", "notes.enex"])
    assert state_directory(args) == Path.cwd()
    args.done_file = Path("relative/progress.txt")
    assert state_directory(args) == Path.cwd()
    args.unsupported_files = tmp_path / "unsupported"
    assert state_directory(args) == tmp_path
    args.done_file = tmp_path / "progress" / "done.txt"
    assert state_directory(args) == tmp_path / "progress"
    args.operations_dir = tmp_path / "operations"
    assert state_directory(args) == tmp_path / "operations"


def test_search_uses_database_compatible_api_version(mocker):
    wrapper = mocker.Mock()
    wrapper._rate_limit_delay = 0
    wrapper._auth_token = "test-token"
    wrapper._retry_on_rate_limit.side_effect = lambda function, **kwargs: function(**kwargs)
    first, second = mocker.Mock(), mocker.Mock()
    database = {"object": "database", "id": "db", "title": [{"plain_text": "Notebook"}]}
    first.json.return_value = {"results": [], "has_more": True, "next_cursor": "next"}
    second.json.return_value = {"results": [database], "has_more": False}
    post = mocker.patch("enex2notion.import_state.requests.post", side_effect=[first, second])

    assert ImportRemote(wrapper).search("Notebook") == [database]
    assert post.call_args.args[0] == "https://api.notion.com/v1/search"
    assert post.call_args.kwargs["headers"]["Notion-Version"] == "2022-06-28"
    assert post.call_args.kwargs["json"]["start_cursor"] == "next"
    wrapper.client.search.assert_not_called()


def test_last_editor_fallback_requires_every_block(importer):
    importer.args.skip_changed_since_import = True
    importer.args.allow_importer_last_edit = True
    importer.importer_id = "importer-bot"
    snapshot = make_snapshot()
    snapshot["page"]["last_edited_by"] = {"id": "importer-bot"}
    snapshot["blocks"][0]["last_edited_by"] = {"id": "importer-bot"}
    snapshot["blocks"][0]["_children"] = [{
        "id": "child", "type": "paragraph", "last_edited_time": "2026-09-01T00:00:00Z",
        "last_edited_by": {"id": "importer-bot"}, "paragraph": {"rich_text": []},
    }]
    importer.remote.snapshot.return_value = snapshot
    assert make_plan(importer)["action"] == "replace"
    assert make_plan(importer)["editor_fallback"] is True
    snapshot["blocks"][0]["_children"][0]["last_edited_by"] = {"id": "human"}
    assert make_plan(importer)["action"] == "skip"
    snapshot["blocks"][0]["_children"][0].pop("last_edited_by")
    assert make_plan(importer)["action"] == "skip"


def test_last_editor_fallback_never_overrides_baseline_or_cutoff(importer):
    importer.args.skip_changed_since_import = True
    importer.args.allow_importer_last_edit = True
    importer.importer_id = "importer-bot"
    snapshot = make_snapshot()
    for value in [snapshot["page"], *snapshot["blocks"]]:
        value["last_edited_by"] = {"id": "importer-bot"}
    importer.remote.snapshot.return_value = snapshot
    importer.args.skip_edited_after = datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert make_plan(importer)["action"] == "skip"
    importer.args.skip_edited_after = None
    importer.state.record("notebook", make_note(), make_snapshot(edited="2026-08-01T00:00:00Z"))
    assert make_plan(importer)["action"] == "skip"


def test_fallback_verifies_integration_id_not_display_name(importer):
    importer.args.skip_changed_since_import = True
    importer.args.allow_importer_last_edit = True
    importer.remote.call.return_value = {"id": "real-bot", "type": "bot", "name": "enex2notion"}
    snapshot = make_snapshot()
    for value in [snapshot["page"], *snapshot["blocks"]]:
        value["last_edited_by"] = {"id": "different-bot", "name": "enex2notion"}
    importer.remote.snapshot.return_value = snapshot
    assert make_plan(importer)["action"] == "skip"
    assert importer.importer_id == "real-bot"


@pytest.mark.parametrize("options", [["--allow-importer-last-edit"], ["--notebook-id", "db"]])
def test_new_options_require_explicit_safeguards(options):
    with pytest.raises(SystemExit):
        parse_args(["--token", "test-token", *options, "notes.enex"])


@pytest.mark.parametrize("parent_id,expected", [("root", "would replace"), ("other-root", "0 would replace")])
def test_explicit_notebook_id_remains_root_scoped(tmp_path, mocker, capsys, parent_id, expected):
    args = parse_args([
        "--token", "test-token", "--reimport", "--whatif", "1", "--mode", "PAGE",
        "--notebook-id", "notebook", "--operations-dir", str(tmp_path), "renamed-export.enex",
    ])
    wrapper = mocker.Mock()
    mocker.patch("enex2notion.cli_reimport.get_notion_wrapper", return_value=wrapper)
    remote = mocker.patch("enex2notion.cli_reimport.ImportRemote").return_value
    remote.search.return_value = [{"id": "root", "object": "page"}]
    remote.page.return_value = {"id": "notebook", "object": "page", "parent": {"page_id": parent_id}}
    remote.notebook_pages.return_value = [make_snapshot()["page"]]
    remote.snapshot.return_value = make_snapshot()
    mocker.patch("enex2notion.cli_reimport.parse_all_notes", return_value=SimpleNamespace(results=[
        SimpleNamespace(note=make_note(), failed=False),
    ]))

    reimport_command(args)

    output = capsys.readouterr().out
    assert expected in output
    remote.search.assert_called_once_with(args.root_page)
    remote.page.assert_called_once_with("notebook")
    if parent_id == "other-root":
        remote.notebook_pages.assert_not_called()
        assert "--mode PAGE" in output and "0 matching notebooks" in output
    else:
        assert "1 would replace" in output
    assert wrapper.mock_calls == []
    assert list(tmp_path.iterdir()) == []


def attachment_note_and_snapshot(kind="pdf", mime="application/pdf", suffix="<div>Replacement</div>"):
    data = b"verified file bytes"
    digest = hashlib.md5(data).hexdigest()
    resource = EvernoteResource(data, len(data), digest, mime, "file.bin")
    note = make_note(content=f'<en-media type="{mime}" hash="{digest}"/>{suffix}')
    note.resources = [resource]
    snapshot = make_snapshot()
    snapshot["blocks"].insert(0, {
        "id": "file-block", "type": kind, "last_edited_time": "2026-09-01T00:00:00Z",
        kind: {"type": "file", "file": {"url": "https://prod-files-secure.s3.us-west-2.amazonaws.com/file.bin?signature=temporary"}},
    })
    return note, snapshot, (len(data), hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize("kind,mime", [
    ("pdf", "application/pdf"), ("image", "image/png"), ("audio", "audio/mpeg"),
    ("video", "video/mp4"), ("file", "application/octet-stream"),
])
def test_verified_files_are_retained_beside_changed_text(importer, mocker, kind, mime):
    note, snapshot, digest = attachment_note_and_snapshot(kind, mime)
    importer.remote.snapshot.return_value = snapshot
    download = mocker.patch("enex2notion.reimport_attachments.hosted_digest", return_value=digest)
    plan = make_plan(importer, note)
    assert plan["action"] == "replace"
    assert plan["retained"] == {0: snapshot["blocks"][0]}
    assert plan["before"]["blocks"][0] == plan["after"]["blocks"][0]
    importer.display("Note", plan)
    assert any("upload 0 attachments" in call.args[0] for call in importer.output.call_args_list)
    make_plan(importer, note)
    download.assert_called_once()
    assert importer.wrapper.mock_calls == []


def test_file_only_note_is_unchanged_after_verification(importer, mocker):
    note, snapshot, digest = attachment_note_and_snapshot(suffix="")
    snapshot["blocks"].pop()
    importer.remote.snapshot.return_value = snapshot
    mocker.patch("enex2notion.reimport_attachments.hosted_digest", return_value=digest)
    assert make_plan(importer, note)["action"] == "unchanged"


@pytest.mark.parametrize("verified", [None, (19, "different-content")])
def test_same_filename_does_not_prove_file_identity(importer, mocker, verified):
    note, snapshot, _ = attachment_note_and_snapshot()
    importer.remote.snapshot.return_value = snapshot
    mocker.patch("enex2notion.reimport_attachments.hosted_digest", return_value=verified)
    plan = make_plan(importer, note)
    assert plan["retained"] == {}
    assert "file_upload" in plan["after"]["blocks"][0]["pdf"]
    importer.display("Note", plan)
    rendered = "\n".join(call.args[0] for call in importer.output.call_args_list)
    assert "signature=temporary" not in rendered
    assert "signature=temporary" in plan["snapshot"]["blocks"][0]["pdf"]["file"]["url"]


@pytest.mark.parametrize("failure", ["rejected", "validation"])
def test_attachment_preparation_failure_leaves_no_recovery_record(importer, mocker, tmp_path, failure):
    note, original, _ = attachment_note_and_snapshot("image", "image/png")
    importer.remote.snapshot.return_value = original
    mocker.patch("enex2notion.reimport_attachments.hosted_digest", return_value=None)
    plan = make_plan(importer, note)
    if failure == "rejected":
        importer.wrapper.upload_file.side_effect = ValueError("File content type rejected")
    else:
        mocker.patch("enex2notion.cli_reimport._process_image_blocks", return_value=[])

    with pytest.raises(RuntimeError, match="original note preserved") as error:
        importer.execute(plan)

    assert note.resources[0].file_name in str(error.value)
    if failure == "rejected":
        assert "File content type rejected" in str(error.value)
    importer.wrapper.append_blocks.assert_not_called()
    importer.remote.archive.assert_not_called()
    importer.remote.update_properties.assert_not_called()
    assert list(tmp_path.iterdir()) == []
    assert make_plan(importer, note)["action"] == "replace"


def test_execution_never_uploads_or_archives_retained_file(importer, mocker):
    note, original, digest = attachment_note_and_snapshot()
    importer.remote.snapshot.return_value = original
    mocker.patch("enex2notion.reimport_attachments.hosted_digest", return_value=digest)
    plan = make_plan(importer, note)
    new = make_snapshot(text="Replacement", edited="2026-09-02T00:00:00Z")["blocks"][0]
    new["id"] = "new"
    staged = copy.deepcopy(original)
    staged["blocks"].insert(1, new)
    completed = copy.deepcopy(staged)
    completed["blocks"].pop()
    importer.remote.snapshot.side_effect = [original, original, staged, completed]
    files = mocker.patch("enex2notion.cli_reimport._process_image_blocks", return_value=[])
    importer.wrapper.append_blocks.return_value = [{"id": "new"}]

    importer.execute(plan)

    assert all(isinstance(block, NotionTextBlock) for block in files.call_args.args[0])
    importer.remote.archive.assert_called_once_with("old")
    assert importer.wrapper.append_blocks.call_args.kwargs["after"] == "file-block"
    assert importer.wrapper.append_blocks.call_args.kwargs["children"][0]["type"] == "paragraph"
    importer.remote.update_properties.assert_not_called()


def test_metadata_only_change_does_not_recreate_files(importer, mocker):
    note, snapshot, digest = attachment_note_and_snapshot(suffix="")
    snapshot["blocks"].pop()
    snapshot["page"]["properties"]["Updated"] = {"date": {"start": "2026-01-01T00:00:00Z"}}
    importer.remote.snapshot.return_value = snapshot
    mocker.patch("enex2notion.reimport_attachments.hosted_digest", return_value=digest)
    plan = importer.plan(note, "notebook", [snapshot["page"]], {"title": {"type": "title"}, "Updated": {"type": "date"}})
    assert set(plan["properties"]) == {"Updated"}
    assert plan["retained"] == {0: snapshot["blocks"][0]}
    files = mocker.patch("enex2notion.cli_reimport._process_image_blocks", return_value=[])
    importer.execute(plan)
    assert files.call_args.args[0] == []
    importer.wrapper.append_blocks.assert_not_called()
    importer.remote.archive.assert_not_called()
    importer.remote.update_properties.assert_called_once_with("page", plan["properties"])


def test_changed_prefix_is_inserted_before_retained_file(importer, mocker):
    note, original, digest = attachment_note_and_snapshot(suffix="")
    note.content = note.content.replace("<en-note>", "<en-note><div>Replacement</div>")
    original["blocks"].reverse()
    importer.remote.snapshot.return_value = original
    mocker.patch("enex2notion.reimport_attachments.hosted_digest", return_value=digest)
    plan = make_plan(importer, note)
    assert plan["retained"] == {1: original["blocks"][1]}
    new = make_snapshot(text="Replacement")["blocks"][0]
    new["id"] = "new"
    staged = copy.deepcopy(original)
    staged["blocks"].insert(1, new)
    completed = copy.deepcopy(staged)
    completed["blocks"].pop(0)
    importer.remote.snapshot.side_effect = [original, original, staged, completed]
    mocker.patch("enex2notion.cli_reimport._process_image_blocks", return_value=[])
    importer.wrapper.append_blocks.return_value = [{"id": "new"}]
    importer.execute(plan)
    assert importer.wrapper.append_blocks.call_args.kwargs["after"] == "old"
    importer.remote.archive.assert_called_once_with("old")


def test_nested_file_retained_when_entire_parent_is_unchanged(importer, mocker):
    note, snapshot, digest = attachment_note_and_snapshot(suffix="")
    note.content = note.content.replace("<en-note>", "<en-note><ul><li>Parent").replace("</en-note>", "</li></ul></en-note>")
    snapshot["blocks"] = [{
        "id": "list", "last_edited_time": "2026-09-01T00:00:00Z", "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": "Parent"}}]},
        "_children": [snapshot["blocks"][0]],
    }]
    importer.remote.snapshot.return_value = snapshot
    mocker.patch("enex2notion.reimport_attachments.hosted_digest", return_value=digest)
    assert make_plan(importer, note)["action"] == "unchanged"
    note.content = note.content.replace("Parent", "Changed parent")
    plan = make_plan(importer, note)
    assert plan["retained"] == {}
    assert "file_upload" in plan["after"]["blocks"][0]["bulleted_list_item"]["children"][0]["pdf"]


def test_hosted_file_verification_is_bounded_and_unauthenticated(mocker):
    response = mocker.Mock()
    response.status_code = 200
    response.iter_content.return_value = [b"abc", b"def"]
    context = mocker.MagicMock()
    context.__enter__.return_value = response
    get = mocker.patch("enex2notion.reimport_attachments.requests.get", return_value=context)
    url = "https://prod-files-secure.s3.us-west-2.amazonaws.com/file"
    assert hosted_digest(url, 6) == (6, hashlib.sha256(b"abcdef").hexdigest())
    assert "headers" not in get.call_args.kwargs and "auth" not in get.call_args.kwargs
    assert get.call_args.kwargs["allow_redirects"] is False
    assert hosted_digest(url, 5) is None
    response.status_code = 403
    assert hosted_digest(url, 6) is None
    get.reset_mock()
    assert hosted_digest("https://untrusted.example/file", 6) is None
    get.assert_not_called()