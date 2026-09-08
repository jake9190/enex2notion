import logging

from bs4 import Tag
from tinycss2 import parse_declaration_list, serialize

from enex2notion.note_parser.string_extractor import extract_string
from enex2notion.notion_blocks.container import NotionCodeBlock, NotionCalloutBlock
from enex2notion.notion_blocks.list import NotionTodoBlock
from enex2notion.notion_blocks.minor import NotionBookmarkBlock
from enex2notion.notion_blocks.text import NotionTextBlock, TextProp
from enex2notion.parse_warnings import add_warning

logger = logging.getLogger(__name__)


def parse_div(element: Tag):
    style = element.get("style", "")

    if "en-task-group" in style:
        return _parse_container(element)

    # Google drive links
    if "en-richlink" in style:
        return parse_richlink(element)

    # Code blocks
    if "en-codeblock" in style:
        return parse_codeblock(element)

    # Embedded webclips (handle as individual elements, not note-level)
    if "en-clipped-content" in style:
        return parse_embedded_webclip(element)

    # Text paragraph
    return parse_text(element)


def parse_codeblock(element: Tag):
    return NotionCodeBlock(text_prop=extract_string(element))


def parse_text(element: Tag):
    element_text = extract_string(element)

    todo = element.find("en-todo")
    if todo:
        is_checked = todo.get("checked") == "true"
        return NotionTodoBlock(text_prop=element_text, checked=is_checked)

    return NotionTextBlock(text_prop=element_text)


def parse_richlink(element: Tag):
    url = _style_value(element, "en-href")
    if not url:
        add_warning("Rich link missing URL; preserved its available content")
        return _parse_container(element)

    return NotionBookmarkBlock(url=url)


def _style_value(element: Tag, name: str):
    for declaration in parse_declaration_list(element.get("style", "")):
        if declaration.type == "declaration" and declaration.name.lstrip("-") == name:
            return serialize(declaration.value).strip().strip("\"'")
    return ""


def _parse_container(element: Tag):
    from enex2notion.note_parser.blocks import parse_note_blocks

    return parse_note_blocks(element)


def parse_embedded_webclip(element: Tag):
    """Preserve the saved clip body alongside its source information."""
    source_url = _style_value(element, "en-clipped-source-url") or "Unknown source"
    source_title = _style_value(element, "en-clipped-source-title") or "Embedded web clip"
    
    # Create a callout with link to the original source
    callout_text = f"📎 {source_title}\nSource: {source_url}"
    
    logger.info(f"Converting embedded webclip to callout: {source_title}")
    
    callout = NotionCalloutBlock(
        icon="📎",
        text_prop=TextProp(callout_text)
    )
    return [callout, *_parse_container(element)]
