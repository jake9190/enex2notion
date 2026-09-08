import copy
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
    importer.remote.update_properties.assert_called_once_with("page", plan["properties"])
    record = importer.state.get("notebook", make_note())
    assert record["page_id"] == "page"
    journal = json.loads(next((tmp_path / "reimport-backups").glob("*.json")).read_text())
    assert journal["status"] == "complete"
    assert journal["original"]["blocks"][0]["id"] == "old"


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


def test_edit_during_upload_prevents_archiving(importer, mocker):
    plan = make_plan(importer)
    original, staged, _ = configure_success(importer, mocker)
    staged["blocks"][0]["last_edited_time"] = "2026-09-03T00:00:00Z"
    importer.remote.snapshot.side_effect = [original, original, staged]
    with pytest.raises(RuntimeError, match="Original content changed"):
        importer.execute(plan)
    importer.remote.archive.assert_not_called()
    importer.remote.update_properties.assert_not_called()


def test_database_properties_leave_custom_fields_alone(importer):
    schema = {
        "Name": {"type": "title"}, "Tags": {"type": "multi_select"},
        "URL": {"type": "url"}, "Personal URL": {"type": "url"},
        "Workflow": {"type": "multi_select"}, "Review date": {"type": "date"},
    }
    plan = importer.plan(make_note(), "notebook", [make_snapshot()["page"]], schema)
    assert set(plan["properties"]) == {"Name", "Tags", "URL"}


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