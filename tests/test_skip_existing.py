from datetime import datetime, timezone

import pytest

from enex2notion.cli_args import parse_args
from enex2notion.cli_upload import EnexUploader
from enex2notion.enex_types import EvernoteNote
from enex2notion.utils_static import Rules


def make_uploader(wrapper, mode="DB", skip_existing=True, tmp_path=None):
    uploader = EnexUploader(
        wrapper=wrapper,
        root_id="root",
        mode=mode,
        done_file=None,
        rules=Rules(False, False, False, None),
        working_dir=tmp_path,
        skip_existing=skip_existing,
    )
    uploader.notebook_root = "notebook"
    return uploader


def make_note(title="Example", updated=None):
    return EvernoteNote(
        title=title,
        created=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated=updated or datetime(2026, 1, 2, tzinfo=timezone.utc),
        content="<en-note>Content</en-note>",
        tags=[],
        author="",
        url="",
        is_webclip=False,
        is_email=False,
        resources=[],
    )


def test_skip_existing_cli_option():
    args = parse_args(["--token", "test-token", "--skip-existing", "notes.enex"])
    assert args.skip_existing is True

    with pytest.raises(SystemExit):
        parse_args(["--token", "test-token", "--skip-existing", "--reimport", "notes.enex"])


def test_db_mode_matches_updated_dates_to_minute_precision(mocker, tmp_path):
    wrapper = mocker.Mock()
    wrapper.list_database_pages.return_value = [{
        "properties": {
            "Title": {"title": [{"plain_text": "Example"}]},
            "Updated": {"date": {"start": "2026-01-02T00:00:00Z"}},
        },
    }]
    uploader = make_uploader(wrapper, tmp_path=tmp_path)
    uploader.notebook_schema = {
        "Title": {"type": "title"},
        "Updated": {"type": "date"},
    }

    uploader._load_existing_signatures()

    assert uploader._signature(
        "Example", datetime(2026, 1, 2, 0, 0, 45, tzinfo=timezone.utc)
    ) in uploader.existing_signatures


def test_page_mode_reads_updated_from_metadata_callout(mocker, tmp_path):
    wrapper = mocker.Mock()
    wrapper.get_blocks.return_value = [{
        "type": "child_page",
        "child_page": {"title": "Example"},
        "_children": [{
            "type": "callout",
            "callout": {"rich_text": [{
                "plain_text": "Created: 2026-01-01\nUpdated: 2026-01-02T00:00:00+00:00",
            }]},
        }],
    }]
    uploader = make_uploader(wrapper, mode="PAGE", tmp_path=tmp_path)

    uploader._load_existing_signatures()

    assert uploader._signature("Example", "2026-01-02T00:00:00Z") in uploader.existing_signatures


def test_matching_existing_signature_skips_upload(mocker, tmp_path):
    wrapper = mocker.Mock()
    uploader = make_uploader(wrapper, tmp_path=tmp_path)
    note = make_note()
    signature = uploader._signature(note.title, note.updated)
    uploader.existing_signatures.add(signature)
    uploader._parse_note = mocker.Mock()

    status, reason = uploader._upload_single_note(note, 1, 1, "notebook", None)

    assert status == "skipped"
    assert "same title and ENEX updated date" in reason
    uploader._parse_note.assert_not_called()
