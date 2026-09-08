from datetime import datetime, timezone

import pytest

from enex2notion.enex_types import EvernoteNote
from enex2notion.enex_uploader import _convert_note_blocks
from enex2notion.note_parser.note import parse_note
from enex2notion.notion_block_converter import _convert_quote, convert_block_to_api_format
from enex2notion.notion_blocks.container import NotionCalloutBlock
from enex2notion.notion_blocks.list import NotionNumberedListBlock
from enex2notion.notion_blocks.text import NotionTextBlock, TextProp
from enex2notion.utils_static import Rules


def parse_content(content, is_email=False, rules=None):
    note = EvernoteNote(
        title="Content preservation",
        created=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
        content=f"<en-note>{content}</en-note>",
        tags=[],
        author="",
        url="",
        is_webclip=False,
        is_email=is_email,
        resources=[],
    )
    return parse_note(note, rules or Rules(False, False, False, None))


def payload_text(value):
    if isinstance(value, dict):
        if value.get("type") == "text" and isinstance(value.get("text"), dict):
            return value["text"]["content"]
        return "".join(payload_text(child) for child in value.values())
    if isinstance(value, list):
        return "".join(payload_text(child) for child in value)
    return ""


def assert_text_limits(value):
    if isinstance(value, dict):
        if "rich_text" in value:
            assert len(value["rich_text"]) <= 100
        if "cells" in value:
            assert all(len(cell) <= 100 for cell in value["cells"])
        if value.get("type") == "text" and isinstance(value.get("text"), dict):
            assert len(value["text"]["content"]) <= 2000
        for child in value.values():
            assert_text_limits(child)
    elif isinstance(value, list):
        for child in value:
            assert_text_limits(child)


@pytest.mark.parametrize("is_email", [False, True])
@pytest.mark.parametrize("sections", [False, True])
def test_table_preserves_headers_and_body(is_email, sections):
    header = "<tr><th>Heading</th><th>Value</th></tr>"
    body = "<tr><th>Label</th><td>Content</td></tr>"
    if sections:
        header = f"<thead>{header}</thead>"
        body = f"<tbody>{body}</tbody>"
    blocks, errors = parse_content(f"<table>{header}{body}</table>", is_email)

    assert errors == []
    assert payload_text(_convert_note_blocks(blocks)) == "HeadingValueLabelContent"


def test_table_preserves_mixed_direct_and_section_rows():
    blocks, errors = parse_content(
        "<table><tr><td>First</td></tr>"
        "<tbody><tr><td>Second</td></tr></tbody></table>"
    )

    assert errors == []
    assert payload_text(_convert_note_blocks(blocks)) == "FirstSecond"


@pytest.mark.parametrize("suffix", ["", ";"])
def test_richlink_accepts_optional_semicolon(suffix):
    blocks, errors = parse_content(
        '<div>Before</div><div style="--en-richlink:true;'
        f'--en-href:https://example.com{suffix}">Saved link</div><div>After</div>'
    )

    assert errors == []
    payload = _convert_note_blocks(blocks)
    assert payload_text(payload) == "BeforeAfter"
    assert payload[1]["bookmark"]["url"] == "https://example.com"


def test_richlink_without_url_preserves_surrounding_content():
    blocks, errors = parse_content(
        '<div>Before</div><div style="--en-richlink:true;">'
        'Fallback<ul><li>Detail</li></ul></div><div>After</div>'
    )

    assert payload_text(_convert_note_blocks(blocks)) == "BeforeFallbackDetailAfter"
    assert any("Rich link missing URL" in error for error in errors)


def test_task_group_preserves_content():
    blocks, errors = parse_content(
        '<div>Before</div><div style="--en-task-group:true;">'
        '<div><en-todo checked="true"/>Task</div>'
        '<ul><li>Detail</li></ul></div><div>After</div>'
    )

    assert errors == []
    payload = _convert_note_blocks(blocks)
    assert payload_text(payload) == "BeforeTaskDetailAfter"
    assert payload[1]["to_do"]["checked"] is True
    assert payload[2]["type"] == "bulleted_list_item"


def test_embedded_clip_preserves_saved_body():
    blocks, errors = parse_content(
        '<div style="--en-clipped-content:true;--en-clipped-source-url:https://example.com;">'
        '<p>Saved text</p><ul><li>Saved item</li></ul>'
        '<table><tr><th>Heading</th></tr><tr><td>Cell</td></tr></table>'
        '<img src="https://example.com/image.png"/></div>'
    )

    assert errors == []
    payload = _convert_note_blocks(blocks)
    assert payload_text(payload).endswith("Saved textSaved itemHeadingCell")
    assert [block["type"] for block in payload] == [
        "callout", "paragraph", "bulleted_list_item", "table", "image",
    ]


def formatted_content(count):
    return "".join(f"<b>{index:03d}</b><i>|</i>" for index in range(count))


@pytest.mark.parametrize("container", [
    "<ul><li>{}</li></ul>",
    "<ol><li>{}</li></ol>",
    '<div><en-todo checked="true"/>{}</div>',
    "<h1>{}</h1>",
    "<h2>{}</h2>",
    "<h3>{}</h3>",
    '<div style="--en-codeblock:true;">{}</div>',
    "<div>{}</div>",
])
def test_formatted_block_overflow_preserves_all_text(container):
    blocks, errors = parse_content(container.format(formatted_content(102)))

    assert errors == []
    payload = _convert_note_blocks(blocks)
    assert payload_text(payload) == "".join(f"{index:03d}|" for index in range(102))
    assert_text_limits(payload)


def test_long_list_item_keeps_continuation_before_nested_list():
    blocks, errors = parse_content(
        "<ol><li>" + formatted_content(51) + "<ul><li>Child</li></ul></li><li>Next</li></ol>"
    )

    assert errors == []
    payload = _convert_note_blocks(blocks)
    assert len(payload) == 2
    assert [child["type"] for child in payload[0]["numbered_list_item"]["children"]] == [
        "paragraph", "bulleted_list_item",
    ]
    assert payload_text(payload) == "".join(f"{index:03d}|" for index in range(51)) + "ChildNext"


@pytest.mark.parametrize("first_row", [False, True])
def test_table_overflow_preserves_text_within_api_limits(first_row):
    header = "" if first_row else "<tr><td>Header</td></tr>"
    blocks, errors = parse_content(
        f"<table>{header}<tr><td>{formatted_content(102)}</td></tr></table>"
    )

    assert errors == []
    payload = _convert_note_blocks(blocks)
    prefix = "" if first_row else "Header"
    assert payload_text(payload) == prefix + "".join(f"{index:03d}|" for index in range(102))
    for block in payload:
        for row in block["table"]["children"]:
            assert all(len(cell) <= 100 for cell in row["table_row"]["cells"])


def test_html_indentation_does_not_create_blank_paragraphs():
    blocks, errors = parse_content("<div>\n  <div>First</div>\n  <div>Second</div>\n</div>")

    assert errors == []
    assert [block.text_prop.text for block in blocks] == ["First", "Second"]


@pytest.mark.parametrize("content,expected", [
    ("<div>First<br/></div><div>Second<br/></div>", "First\nSecond"),
    ("<div>First</div><div><br/></div><div>Second</div>", "First\n\nSecond"),
    ("<div>Same</div><div>Same</div>", "Same\nSame"),
    ("First\n  second", "First second"),
    ("First<br/><br/>Second", "First\n\nSecond"),
])
def test_list_text_has_only_rendered_line_breaks(content, expected):
    blocks, errors = parse_content(f"<ul><li>{content}</li></ul>")

    assert errors == []
    assert blocks[0].text_prop.text == expected


def test_paragraph_placeholder_breaks_do_not_add_spacing():
    blocks, errors = parse_content("<div>First<br/></div><div>Second<br/></div>")

    assert errors == []
    assert [block.text_prop.text for block in blocks] == ["First", "Second"]


def test_intentional_blank_paragraph_is_preserved():
    blocks, errors = parse_content("<div>First</div><div><br/></div><div>Second</div>")

    assert errors == []
    assert [block.text_prop.text for block in blocks] == ["First", "", "Second"]


@pytest.mark.parametrize("container", [
    '<div style="--en-codeblock:true;">{}</div>',
    "<pre>{}</pre>",
    '<div style="white-space: pre-wrap;">{}</div>',
])
def test_preformatted_whitespace_is_preserved(container):
    text = "First\n\n  indented\n"
    blocks, errors = parse_content(container.format(text))

    assert errors == []
    assert blocks[0].text_prop.text == text


@pytest.mark.parametrize("block_kind", ["callout", "quote", "numbered_list"])
def test_long_plain_text_preserves_overflow(block_kind):
    text = "a" * 200_001
    text_prop = TextProp(text)
    if block_kind == "callout":
        block = NotionCalloutBlock(icon=chr(0x1F4A1), text_prop=text_prop)
        payload = convert_block_to_api_format(block)
        assert isinstance(payload, dict)
    elif block_kind == "quote":
        payload = _convert_quote(NotionTextBlock(text_prop=text_prop))
    else:
        payload = convert_block_to_api_format(NotionNumberedListBlock(text_prop=text_prop))

    assert payload_text(payload) == text
    assert_text_limits(payload)


def test_table_overflow_stays_valid_across_table_splits():
    content = "<table><tr><th>Header</th></tr>" + "".join(
        f"<tr><td>Row {index}</td></tr>" for index in range(98)
    ) + f"<tr><td>{formatted_content(102)}</td></tr></table>"
    blocks, errors = parse_content(content)

    assert errors == []
    payload = _convert_note_blocks(blocks)
    tables = [block for block in payload if block["type"] == "table"]
    assert len(tables) == 2
    assert all(len(block["table"]["children"]) <= 100 for block in tables)
    assert_text_limits(payload)
    data_rows = [row for block in tables for row in block["table"]["children"][1:]]
    assert payload_text(data_rows) == "".join(f"Row {index}" for index in range(98)) + "".join(
        f"{index:03d}|" for index in range(102)
    )


@pytest.mark.parametrize("sparse", [False, True])
def test_condensing_does_not_double_placeholder_breaks(sparse):
    blocks, errors = parse_content(
        "<div>First<br/></div><div>Second<br/></div>",
        rules=Rules(False, not sparse, sparse, None),
    )

    assert errors == []
    assert [block.text_prop.text for block in blocks] == ["First\nSecond"]


def test_extracted_nested_list_leaves_no_whitespace_line():
    blocks, errors = parse_content(
        "<ul><li>Parent<div>\n <ol><li>Child</li></ol>\n </div></li></ul>"
    )

    assert errors == []
    assert blocks[0].text_prop.text == "Parent"
    assert blocks[0].children[0].text_prop.text == "Child"


def test_whitespace_only_paragraph_is_not_a_visible_blank_line():
    blocks, errors = parse_content("<div>First</div><div> \n </div><div>Second</div>")

    assert errors == []
    assert [block.text_prop.text for block in blocks] == ["First", "Second"]