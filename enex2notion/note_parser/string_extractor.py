import copy
import re

from bs4 import NavigableString, Tag
from tinycss2 import parse_declaration_list, serialize

from enex2notion.note_parser.string_extractor_properties import (
    resolve_string_properties,
)
from enex2notion.note_parser.string_extractor_split_tag import split_tag
from enex2notion.notion_blocks.text import TextProp


def extract_string(tag: Tag) -> TextProp:
    """Convert a block content into a string with properties

     IN: <div>some text <b>bold <i>bold and italic</i></b></div>
    OUT: some text bold bold and italic
         [["some text "], ["bold ", ["b"]], ["bold and italic", ["b", "i"]]]
    """

    preserve_whitespace = _preserves_whitespace(tag)
    div_lines = split_tag(copy.copy(tag))

    string_blocks = _extract_blocks(div_lines, preserve_whitespace)

    result_properties, result_string = _format_blocks(string_blocks)

    return TextProp(result_string, result_properties)


def _extract_blocks(div_lines, preserve_whitespace=False):
    """Get parent stack for each string in the line and convert them to properties

    IN: <div>some text <b>bold</b></div>
    OUT: [
            {"string": "some text", "properties": set()},
            {"string": "bold", "properties": {("b",)}},
         ]
    """

    string_blocks = []
    for index, div in enumerate(div_lines):
        if not preserve_whitespace:
            _normalize_html_whitespace(div)
        _convert_newlines(div)
        for string in div.strings:
            parent_stack = _parents_upto(string, div)
            string_properties = resolve_string_properties(parent_stack)

            _add_string_block(string_blocks, string, string_properties)

        # Add linebreak after each "line"
        # to render embedded lists in tables
        # skip last to avoid trailing linebreak
        if index < len(div_lines) - 1:
            _add_string_block(string_blocks, "\n", set())
    return string_blocks


def _preserves_whitespace(element):
    tags = [element, *element.parents]
    for tag in tags:
        if not isinstance(tag, Tag):
            continue
        if tag.name == "pre" or "en-codeblock" in tag.get("style", ""):
            return True
        for declaration in parse_declaration_list(tag.get("style", "")):
            if declaration.type == "declaration" and declaration.lower_name == "white-space":
                if serialize(declaration.value).strip() in {"pre", "pre-wrap", "pre-line", "break-spaces"}:
                    return True
    return False


def _normalize_html_whitespace(element):
    for string in list(element.strings):
        if not _preserves_whitespace(string):
            string.replace_with(re.sub(r"[ \t]*[\r\n\f\t]+[ \t\r\n\f]*", " ", str(string)))

    leaves = [
        child for child in element.descendants
        if (isinstance(child, NavigableString) and child.strip(" \t\r\n\f"))
        or (isinstance(child, Tag) and not child.contents)
    ]
    if leaves and isinstance(leaves[-1], Tag) and leaves[-1].name == "br":
        if not _preserves_whitespace(leaves[-1]):
            leaves[-1].extract()


def _convert_newlines(element: Tag):
    for br in element.find_all("br"):
        br.replace_with("\n")


def _parents_upto(tag: Tag, upto: Tag):
    parents = []
    for p in tag.parents:
        parents.append(p)
        if p == upto:
            break
    return parents


def _add_string_block(string_blocks, string, string_properties):
    if string_blocks and string_blocks[-1]["properties"] == string_properties:
        string_blocks[-1]["string"] += str(string)
    else:
        string_blocks.append({"string": str(string), "properties": string_properties})


def _format_blocks(string_blocks):
    """Notion properties format:

    plain text: ["some text"]
    formatted text: ["some text", ["property"]]
    multiple properties: ["some text", ["property1", "property2"]]
    property with args: ["some text", [["property", "arg"]]]
    """

    result_properties = []
    for block in string_blocks:
        if block["properties"]:
            properties = [list(p) for p in block["properties"]]
            properties.sort(key=lambda p: "".join(p))

            result_properties.append([block["string"], properties])
        else:
            result_properties.append([block["string"]])
    result_string = "".join(b["string"] for b in string_blocks)

    if not result_string.strip():
        return [], ""

    return result_properties, result_string
