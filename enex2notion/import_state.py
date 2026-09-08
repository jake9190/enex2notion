import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import requests

from enex2notion.notion_api_wrapper import NOTION_API_VERSION


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Notion edit timestamps must include a timezone")
    return parsed


def object_title(value):
    parts = value.get("title", [])
    if not parts:
        for prop in value.get("properties", {}).values():
            if "title" in prop:
                parts = prop["title"]
                break
    return "".join(part.get("plain_text", part.get("text", {}).get("content", "")) for part in parts)


def same_id(first, second):
    return str(first).replace("-", "") == str(second).replace("-", "")


def belongs_to(value, parent_id):
    parent = value.get("parent", {})
    if parent_id is None:
        return parent.get("type") == "workspace"
    return any(same_id(parent.get(key), parent_id) for key in ("page_id", "database_id"))


class ImportRemote:
    def __init__(self, wrapper):
        self.wrapper = wrapper

    def call(self, function, **kwargs):
        time.sleep(self.wrapper._rate_limit_delay)
        return self.wrapper._retry_on_rate_limit(function, **kwargs)

    def paginate(self, function, **kwargs):
        results = []
        while True:
            response = self.call(function, **kwargs)
            results.extend(response.get("results", []))
            if not response.get("has_more"):
                return results
            cursor = response.get("next_cursor")
            if not cursor or cursor == kwargs.get("start_cursor"):
                raise RuntimeError("Notion pagination did not advance")
            kwargs["start_cursor"] = cursor

    def search(self, title):
        return [
            item for item in self.paginate(self.wrapper.client.search, query=title, page_size=100)
            if object_title(item) == title and not item.get("archived") and not item.get("in_trash")
        ]

    def page(self, page_id):
        return self.call(self.wrapper.client.pages.retrieve, page_id=page_id)

    def children(self, block_id):
        return self.paginate(self.wrapper.client.blocks.children.list, block_id=block_id, page_size=100)

    def notebook_pages(self, notebook_id, mode):
        if mode == "PAGE":
            return [self.page(block["id"]) for block in self.children(notebook_id) if block["type"] == "child_page"]

        def query(**body):
            response = requests.post(
                f"https://api.notion.com/v1/databases/{notebook_id}/query",
                json=body,
                headers={"Notion-Version": NOTION_API_VERSION, "Authorization": f"Bearer {self.wrapper._auth_token}"},
                timeout=60,
            )
            response.raise_for_status()
            return response.json()

        return self.paginate(query, page_size=100)

    def snapshot(self, page_id):
        def tree(parent_id):
            blocks = self.children(parent_id)
            for block in blocks:
                if block.get("has_children"):
                    if block["type"] in {"child_page", "child_database", "synced_block", "unsupported"}:
                        raise ValueError(f"Cannot safely replace {block['type']} content")
                    block["_children"] = tree(block["id"])
            return blocks

        page = self.page(page_id)
        if page.get("archived") or page.get("in_trash"):
            raise ValueError("Page is archived or in trash")
        blocks = tree(page_id)
        after = self.page(page_id)
        if page.get("last_edited_time") != after.get("last_edited_time"):
            raise ValueError("Page changed while reading it; retry later")
        return {"page": after, "blocks": blocks}

    def archive(self, block_id, archived=True):
        return self.call(self.wrapper.client.blocks.update, block_id=block_id, archived=archived)

    def update_properties(self, page_id, properties):
        return self.call(self.wrapper.client.pages.update, page_id=page_id, properties=properties)


def edit_signature(snapshot):
    entries = []

    def visit(value):
        entries.append((value["id"], value["last_edited_time"]))
        for child in value.get("_children", []):
            visit(child)

    visit(snapshot["page"])
    for block in snapshot["blocks"]:
        visit(block)
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def latest_edit(snapshot):
    def edits(value):
        yield timestamp(value["last_edited_time"])
        for child in value.get("_children", []):
            yield from edits(child)

    values = list(edits(snapshot["page"]))
    for block in snapshot["blocks"]:
        values.extend(edits(block))
    return max(values)


def source_key(notebook_id, note):
    identity = [str(notebook_id).replace("-", ""), note.title, note.created.isoformat()]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=True, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class ImportState:
    def __init__(self, directory):
        self.path = Path(directory) / "import-state.json"
        self.records = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def get(self, notebook_id, note):
        return self.records.get(source_key(notebook_id, note))

    def record(self, notebook_id, note, snapshot):
        self.records[source_key(notebook_id, note)] = {
            "page_id": snapshot["page"]["id"],
            "signature": edit_signature(snapshot),
            "last_edited_time": latest_edit(snapshot).isoformat(),
            "note_hash": note.note_hash,
        }
        write_json(self.path, self.records)