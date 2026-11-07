import json

import httpx
import pytest
from notion_client import Client

from enex2notion.enex_types import EvernoteResource
from enex2notion.enex_uploader import _process_image_blocks, _upload_block_tree
from enex2notion.notion_api_wrapper import NOTION_API_VERSION, NotionAPIWrapper
from enex2notion.notion_blocks.uploadable import NotionFileBlock


@pytest.mark.parametrize("method", ["_upload_file_singlepart", "_upload_file_multipart"])
@pytest.mark.parametrize("creation_fields,source_type,expected_type", [
    ({"content_type": "image/png"}, "application/octet-stream", "image/png"),
    ({"content_type": "image/png"}, "image/jpeg", "image/png"),
    ({"content_type": "application/octet-stream"}, "image/png", "application/octet-stream"),
    ({"content_type": None}, "application/pdf", "application/pdf"),
    ({"content_type": ""}, "application/pdf", "application/pdf"),
    ({}, "application/pdf", "application/pdf"),
])
def test_file_send_uses_content_type_from_creation(mocker, method, creation_fields, source_type, expected_type):
    file_data = b"\x89PNG\r\n\x1a\nimage bytes"
    sends = []

    def post(url, **options):
        if url.endswith("/file_uploads"):
            return httpx.Response(200, json={
                "id": "upload", "part_urls": ["unused"], **creation_fields,
            })
        if url.endswith("/send"):
            sends.append(options["files"]["file"])
            if options["files"]["file"][2] != expected_type:
                return httpx.Response(400, json={"message": f"Current file content type does not match {expected_type}"})
        return httpx.Response(200, json={"id": "upload"})

    mocker.patch("enex2notion.notion_api_wrapper.requests.post", side_effect=post)
    wrapper = NotionAPIWrapper("test-token")
    wrapper._rate_limit_delay = 0
    result = getattr(wrapper, method)(file_data, "photo.png", source_type)

    assert result == "upload"
    assert sends == [("photo.png", file_data, expected_type)]


@pytest.mark.parametrize("rate_limited_stage", ["create", "send"])
def test_singlepart_upload_retries_429_and_honors_retry_after(mocker, rate_limited_stage):
    calls = []
    sleeps = []
    rate_limit_returned = False

    def post(url, **options):
        nonlocal rate_limit_returned
        stage = "send" if url.endswith("/send") else "create"
        calls.append(stage)
        if stage == rate_limited_stage and not rate_limit_returned:
            rate_limit_returned = True
            return httpx.Response(
                429,
                json={"message": "You have been rate limited."},
                headers={"Retry-After": "2"},
            )
        if stage == "create":
            return httpx.Response(200, json={"id": "upload"})
        return httpx.Response(200, json={"status": "uploaded"})

    mocker.patch("enex2notion.notion_api_wrapper.requests.post", side_effect=post)
    mocker.patch("enex2notion.notion_api_wrapper.time.sleep", side_effect=sleeps.append)
    wrapper = NotionAPIWrapper("test-token")
    wrapper._rate_limit_delay = 0

    assert wrapper._upload_file_singlepart(b"image", "photo.png", "image/png") == "upload"
    assert calls.count(rate_limited_stage) == 2
    assert 2 in sleeps


def test_generic_enex_file_gets_upload_id_with_notion_image_content_type(mocker):
    file_data = b"\x89PNG\r\n\x1a\nimage bytes"
    resource = EvernoteResource(file_data, len(file_data), "hash", "application/octet-stream", "photo.png")
    block = NotionFileBlock(md5_hash="hash", resource=resource)

    def post(url, **options):
        if url.endswith("/file_uploads"):
            return httpx.Response(200, json={"id": "upload", "content_type": "image/png"})
        assert options["files"]["file"] == ("photo.png", file_data, "image/png")
        return httpx.Response(200, json={"id": "upload", "status": "uploaded"})

    request = mocker.patch("enex2notion.notion_api_wrapper.requests.post", side_effect=post)
    wrapper = NotionAPIWrapper("test-token")
    wrapper._rate_limit_delay = 0
    warnings = _process_image_blocks([block], wrapper)

    assert warnings == []
    assert block.attrs["file_upload_id"] == "upload"
    assert not block.attrs.get("upload_failed")
    assert resource.mime == "application/octet-stream"
    assert request.call_count == 2


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