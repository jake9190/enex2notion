from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from enex2notion.enex_types import EvernoteNote
from enex2notion.enex_uploader import _convert_note_blocks, _process_image_blocks, _upload_block_tree, _upload_note
from enex2notion.note_parser.blocks import parse_note_blocks
from enex2notion.notion_blocks.uploadable import NotionImageBlock


def _make_note(content):
    return EvernoteNote(
        title="Nested lists",
        created=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
        content=content,
        tags=[],
        author="",
        url="",
        is_webclip=False,
        is_email=False,
        resources=[],
    )


@pytest.mark.parametrize("wrapper_tag", ["div", "span"])
@pytest.mark.parametrize("list_tag,child_tag", [("ul", "ol"), ("ol", "ul")])
def test_upload_nested_lists(parse_html, wrapper_tag, list_tag, child_tag):
    content = (
        f"<{list_tag}><li><{wrapper_tag}>parent<{child_tag}><li>child"
        f"<{list_tag}><li>grandchild</li></{list_tag}></li></{child_tag}>"
        f"</{wrapper_tag}></li><li>sibling</li></{list_tag}>"
    )
    note = _make_note(content)
    wrapper = Mock()
    wrapper.create_page.return_value = {"id": "page"}
    wrapper.append_blocks.side_effect = [
        [{"id": "parent"}, {"id": "sibling"}],
        [{"id": "child"}],
        [{"id": "grandchild"}],
    ]

    result = _upload_note(
        wrapper, "root", note, parse_note_blocks(parse_html(content)),
        [], False, None, None, "notebook",
    )

    assert result[1] is False
    calls = wrapper.append_blocks.call_args_list
    assert [call.kwargs["block_id"] for call in calls] == ["page", "parent", "child"]
    assert [
        [block[block["type"]]["rich_text"][0]["text"]["content"] for block in call.kwargs["children"]]
        for call in calls
    ] == [["parent", "sibling"], ["child"], ["grandchild"]]
    list_type = "bulleted_list_item" if list_tag == "ul" else "numbered_list_item"
    child_type = "bulleted_list_item" if child_tag == "ul" else "numbered_list_item"
    assert [
        [block["type"] for block in call.kwargs["children"]] for call in calls
    ] == [[list_type, list_type], [child_type], [list_type]]


def test_upload_large_nested_list(parse_html):
    content = "<ul><li>parent<ol>" + "".join(
        f"<li>child {index}</li>" for index in range(205)
    ) + "</ol></li></ul>"
    api_blocks = _convert_note_blocks(parse_note_blocks(parse_html(content)))
    wrapper = Mock()
    wrapper.append_blocks.side_effect = lambda *, block_id, children: [
        {"id": block[block["type"]]["rich_text"][0]["text"]["content"]} for block in children
    ]

    _upload_block_tree(wrapper, "page", api_blocks)

    calls = wrapper.append_blocks.call_args_list
    assert [call.kwargs["block_id"] for call in calls] == ["page", "parent", "parent", "parent"]
    assert [len(call.kwargs["children"]) for call in calls] == [1, 100, 100, 5]
    assert [
        block["numbered_list_item"]["rich_text"][0]["text"]["content"]
        for call in calls[1:] for block in call.kwargs["children"]
    ] == [f"child {index}" for index in range(205)]
    assert all(
        "children" not in block[block["type"]]
        for call in calls for block in call.kwargs["children"]
    )


def test_nested_upload_retry_does_not_duplicate_parent(parse_html, mocker):
    sleep = mocker.patch("enex2notion.enex_uploader.time.sleep")
    api_blocks = _convert_note_blocks(parse_note_blocks(parse_html(
        "<ul><li>parent<ol><li>child</li></ol></li></ul>"
    )))
    wrapper = Mock()
    wrapper.append_blocks.side_effect = [
        [{"id": "parent"}], RuntimeError("503 unavailable"), [{"id": "child"}],
    ]

    _upload_block_tree(wrapper, "page", api_blocks)

    assert [call.kwargs["block_id"] for call in wrapper.append_blocks.call_args_list] == [
        "page", "parent", "parent",
    ]
    sleep.assert_called_once_with(0.5)


def test_nested_upload_failure_marks_partial_import(parse_html):
    content = "<ul><li>parent<ol><li>child</li></ol></li></ul>"
    wrapper = Mock()
    wrapper.create_page.return_value = {"id": "page"}
    wrapper.append_blocks.side_effect = [[{"id": "parent"}], RuntimeError("invalid child")]

    result = _upload_note(
        wrapper, "root", _make_note(content), parse_note_blocks(parse_html(content)),
        [], False, None, None, "notebook",
    )

    assert result[0] == "page"
    assert result[1] is True
    assert "Failed to upload blocks: invalid child" in result[2]
    assert wrapper.append_blocks.call_count == 2


def test_upload_table_keeps_inline_rows(parse_html):
    api_blocks = _convert_note_blocks(parse_note_blocks(parse_html(
        "<table><tr><td>cell</td></tr></table>"
    )))
    wrapper = Mock()
    wrapper.append_blocks.return_value = [{"id": "table"}]

    _upload_block_tree(wrapper, "page", api_blocks)

    wrapper.append_blocks.assert_called_once()
    table = wrapper.append_blocks.call_args.kwargs["children"][0]["table"]
    assert table["children"][0]["type"] == "table_row"
    assert table["children"][0]["table_row"]["cells"][0][0]["text"]["content"] == "cell"


def test_inserted_batches_advance_anchor(parse_html):
    blocks = _convert_note_blocks(parse_note_blocks(parse_html(
        "".join(f"<div>{index}</div>" for index in range(205))
    )))
    wrapper = Mock()
    wrapper.append_blocks.side_effect = lambda **kwargs: [
        {"id": block["paragraph"]["rich_text"][0]["text"]["content"]}
        for block in kwargs["children"]
    ]
    _upload_block_tree(wrapper, "page", blocks, after="retained")
    assert [call.kwargs["after"] for call in wrapper.append_blocks.call_args_list] == [
        "retained", "99", "199",
    ]


def test_short_upload_response_keeps_returned_ids_and_stops_children():
    wrapper = Mock()
    wrapper.append_blocks.return_value = [{"id": "partial"}]
    blocks = [{"type": "paragraph", "paragraph": {"rich_text": [], "children": [
        {"type": "paragraph", "paragraph": {"rich_text": []}},
    ]}} for _ in range(2)]
    roots = []
    with pytest.raises(RuntimeError, match="expected 2, received 1"):
        _upload_block_tree(wrapper, "page", blocks, uploaded_roots=roots, after="retained")
    assert roots == [{"id": "partial"}]
    wrapper.append_blocks.assert_called_once()


@pytest.mark.parametrize("reason", [
    "File content type image/png does not match extension .jpg",
    "File extension that is not supported",
    "Expected 2 part URLs, got 0",
])
def test_rejected_image_without_disk_fallback_reports_reason(reason):
    resource = Mock(data_bin=b"image", file_name="photo.jpg", mime="image/png")
    block = NotionImageBlock(md5_hash="hash", resource=resource)
    wrapper = Mock()
    wrapper.upload_file.side_effect = ValueError(reason)

    warnings = _process_image_blocks([block], wrapper)

    assert any("photo.jpg" in warning and reason in warning for warning in warnings)
    assert block.attrs["upload_failed"] is True
    assert "file_upload_id" not in block.attrs
    wrapper.upload_file.assert_called_once()


def test_upload_worker_exception_reports_filename(mocker):
    block = NotionImageBlock(md5_hash="hash", resource=Mock(file_name="photo.jpg"))
    mocker.patch("enex2notion.enex_uploader._upload_single_file", side_effect=ValueError("worker failure"))
    warnings = _process_image_blocks([block], Mock())
    assert warnings == ["File upload failed: 'photo.jpg' - worker failure"]
    assert block.attrs["upload_failed"] is True


@pytest.mark.parametrize("saved", [False, True])
def test_rejected_image_fallback_only_suppresses_warning_when_saved(mocker, tmp_path, saved):
    block = NotionImageBlock(md5_hash="hash", resource=Mock(
        data_bin=b"image", file_name="photo.jpg", mime="image/png",
    ))
    wrapper = Mock()
    wrapper.upload_file.side_effect = ValueError("File content type rejected")
    saved_path = tmp_path / "photo.jpg" if saved else None
    save = mocker.patch("enex2notion.image_handler._save_unsupported_file", return_value=saved_path)
    failed_uploads = []
    warnings = _process_image_blocks(
        [block], wrapper, unsupported_dir=tmp_path, failed_uploads_list=failed_uploads,
    )
    save.assert_called_once()
    if saved:
        assert warnings == []
        assert failed_uploads == [{"filename": "photo.jpg", "path": str(saved_path)}]
    else:
        assert any("photo.jpg" in warning and "File content type rejected" in warning for warning in warnings)
        assert failed_uploads == []


@pytest.mark.parametrize("missing_resource", [False, True])
def test_missing_image_data_reports_warning_without_upload(missing_resource):
    resource = None if missing_resource else Mock(data_bin=b"", file_name="empty.png")
    block = NotionImageBlock(md5_hash="hash", resource=resource)
    wrapper = Mock()
    warnings = _process_image_blocks([block], wrapper)
    assert any("cannot upload" in warning for warning in warnings)
    if not missing_resource:
        assert any("empty.png" in warning for warning in warnings)
    wrapper.upload_file.assert_not_called()


def test_missing_upload_id_reports_filename():
    block = NotionImageBlock(md5_hash="hash", resource=Mock(
        data_bin=b"image", file_name="photo.png", mime="image/png",
    ))
    wrapper = Mock()
    wrapper.upload_file.return_value = None
    warnings = _process_image_blocks([block], wrapper)
    assert any("photo.png" in warning and "no file upload ID" in warning for warning in warnings)
    wrapper.upload_file.assert_called_once()