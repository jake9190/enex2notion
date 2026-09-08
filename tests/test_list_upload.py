from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from enex2notion.enex_types import EvernoteNote
from enex2notion.enex_uploader import _convert_note_blocks, _upload_block_tree, _upload_note
from enex2notion.note_parser.blocks import parse_note_blocks


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