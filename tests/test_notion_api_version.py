import json

import httpx
import pytest
from notion_client import Client

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