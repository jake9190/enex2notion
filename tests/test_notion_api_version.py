import httpx
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