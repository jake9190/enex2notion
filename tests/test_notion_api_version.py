import json

import httpx
import pytest
from notion_client import Client

from enex2notion.enex_uploader import _upload_block_tree
from enex2notion.notion_api_wrapper import NOTION_API_VERSION, NotionAPIWrapper


def test_database_schema_requests_use_legacy_api_version(mocker):
    requests = []

    def respond(request):
        requests.append(request)
        body = {"object": "database", "id": "notebook"}
        if request.headers.get("Notion-Version") == NOTION_API_VERSION:
            body["properties"] = {"Name": {"type": "title", "title": {}}}
        else:
            body["data_sources"] = [{"id": "source", "name": "Notebook"}]
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        mocker.patch(
            "enex2notion.notion_api_wrapper.Client",
            side_effect=lambda **options: Client(client=http_client, **options),
        )
        wrapper = NotionAPIWrapper("test-token")
        wrapper._rate_limit_delay = 0
        database = wrapper.get_database("notebook")

    assert "properties" in database
    assert requests[0].headers["Notion-Version"] == NOTION_API_VERSION


def test_sdk_passes_insertion_anchor_and_advances_batches(mocker):
    bodies = []

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"results": [{"id": "existing-file"}], "has_more": False})
        body = json.loads(request.content)
        bodies.append(body)
        assert request.headers["Notion-Version"] == NOTION_API_VERSION
        return httpx.Response(200, json={"results": [
            {"id": block["paragraph"]["rich_text"][0]["text"]["content"]}
            for block in body["children"]
        ]})

    blocks = [{"type": "paragraph", "paragraph": {"rich_text": [
        {"type": "text", "text": {"content": str(index)}},
    ]}} for index in range(205)]
    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        mocker.patch(
            "enex2notion.notion_api_wrapper.Client",
            side_effect=lambda **options: Client(client=http_client, **options),
        )
        wrapper = NotionAPIWrapper("test-token")
        wrapper._rate_limit_delay = 0
        result = wrapper.append_blocks("page", blocks, after="existing-file")

    assert len(result) == 205
    assert [body["after"] for body in bodies] == ["existing-file", "99", "199"]


@pytest.mark.parametrize("returned_count", [0, 1, 3])
def test_sdk_unexpected_count_stops_later_batches(mocker, returned_count):
    requests = []
    returned = [{"id": str(index)} for index in range(returned_count)]

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"results": [{"id": "existing"}], "has_more": False})
        requests.append(request)
        return httpx.Response(200, json={"results": returned})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        mocker.patch(
            "enex2notion.notion_api_wrapper.Client",
            side_effect=lambda **options: Client(client=http_client, **options),
        )
        wrapper = NotionAPIWrapper("test-token")
        wrapper._rate_limit_delay = 0
        blocks = [{"type": "paragraph", "paragraph": {"rich_text": []}} for _ in range(4)]
        result = wrapper.append_blocks("page", blocks, max_batch=2, after="existing")

    assert result == returned
    assert len(requests) == 1


def test_anchored_append_excludes_existing_siblings(mocker):
    requests = []
    original = [{"id": f"old-{index}"} for index in range(10)]
    new_block = {"id": "new", "type": "paragraph", "paragraph": {"rich_text": []}}

    def respond(request):
        requests.append(request)
        if request.method == "GET":
            if request.url.params.get("start_cursor") == "next":
                return httpx.Response(200, json={"results": original[5:], "has_more": False})
            return httpx.Response(200, json={"results": original[:5], "has_more": True, "next_cursor": "next"})
        return httpx.Response(200, json={"results": [new_block, *original[1:]], "has_more": False})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        mocker.patch(
            "enex2notion.notion_api_wrapper.Client",
            side_effect=lambda **options: Client(client=http_client, **options),
        )
        wrapper = NotionAPIWrapper("test-token")
        wrapper._rate_limit_delay = 0
        result = wrapper.append_blocks(
            "page", [{"type": "paragraph", "paragraph": {"rich_text": []}}], after="old-0",
        )

    assert result == [new_block]
    assert [request.method for request in requests] == ["GET", "GET", "PATCH"]


def test_sibling_responses_advance_only_new_anchors(mocker):
    bodies = []
    siblings = [{"id": "anchor"}, {"id": "tail"}]

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"results": list(siblings), "has_more": False})
        body = json.loads(request.content)
        bodies.append(body)
        new = [{"id": block["paragraph"]["rich_text"][0]["text"]["content"]} for block in body["children"]]
        position = next(index for index, block in enumerate(siblings) if block["id"] == body["after"]) + 1
        siblings[position:position] = new
        return httpx.Response(200, json={"results": list(siblings), "has_more": False})

    blocks = [{"type": "paragraph", "paragraph": {"rich_text": [
        {"type": "text", "text": {"content": str(index)}},
    ]}} for index in range(5)]
    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        mocker.patch(
            "enex2notion.notion_api_wrapper.Client",
            side_effect=lambda **options: Client(client=http_client, **options),
        )
        wrapper = NotionAPIWrapper("test-token")
        wrapper._rate_limit_delay = 0
        result = wrapper.append_blocks("page", blocks, max_batch=2, after="anchor")

    assert [block["id"] for block in result] == [str(index) for index in range(5)]
    assert [body["after"] for body in bodies] == ["anchor", "1", "3"]
    assert [block["id"] for block in siblings] == ["anchor", "0", "1", "2", "3", "4", "tail"]


def test_sibling_response_attaches_children_to_new_parent(mocker):
    paths = []

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"results": [{"id": "old"}], "has_more": False})
        paths.append(request.url.path)
        if len(paths) == 1:
            return httpx.Response(200, json={"results": [{"id": "old"}, {"id": "new"}]})
        return httpx.Response(200, json={"results": [{"id": "child"}]})

    roots = []
    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        mocker.patch(
            "enex2notion.notion_api_wrapper.Client",
            side_effect=lambda **options: Client(client=http_client, **options),
        )
        wrapper = NotionAPIWrapper("test-token")
        wrapper._rate_limit_delay = 0
        _upload_block_tree(wrapper, "page", [{"type": "paragraph", "paragraph": {
            "rich_text": [], "children": [{"type": "paragraph", "paragraph": {"rich_text": []}}],
        }}], uploaded_roots=roots, after="old")

    assert paths == ["/v1/blocks/page/children", "/v1/blocks/new/children"]
    assert roots == [{"id": "new"}]


@pytest.mark.parametrize("new_count", [0, 2])
def test_filtering_siblings_still_rejects_wrong_new_count(mocker, new_count):
    new_blocks = [{"id": f"new-{index}"} for index in range(new_count)]
    writes = []

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"results": [{"id": "old"}], "has_more": False})
        writes.append(request)
        return httpx.Response(200, json={"results": [{"id": "old"}, *new_blocks]})

    roots = []
    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        mocker.patch(
            "enex2notion.notion_api_wrapper.Client",
            side_effect=lambda **options: Client(client=http_client, **options),
        )
        wrapper = NotionAPIWrapper("test-token")
        wrapper._rate_limit_delay = 0
        with pytest.raises(RuntimeError, match=f"expected 1, received {new_count}"):
            _upload_block_tree(wrapper, "page", [
                {"type": "paragraph", "paragraph": {"rich_text": []}},
            ], uploaded_roots=roots, after="old")

    assert roots == new_blocks
    assert len(writes) == 1


def test_incomplete_child_inventory_prevents_append(mocker):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"results": [{"id": "old"}], "has_more": True, "next_cursor": None})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        mocker.patch(
            "enex2notion.notion_api_wrapper.Client",
            side_effect=lambda **options: Client(client=http_client, **options),
        )
        wrapper = NotionAPIWrapper("test-token")
        wrapper._rate_limit_delay = 0
        with pytest.raises(RuntimeError, match="invalid pagination cursor"):
            wrapper.append_blocks("page", [
                {"type": "paragraph", "paragraph": {"rich_text": []}},
            ], after="old")

    assert [request.method for request in requests] == ["GET"]