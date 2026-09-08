import copy
import hashlib
from difflib import SequenceMatcher
import json
from urllib.parse import urlsplit

import requests


FILE_TYPES = {"image", "pdf", "file", "audio", "video"}


def hosted_digest(url, max_size):
    """Read only a bounded, Notion-hosted attachment; never forward API credentials."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not any(
        host == domain or host.endswith("." + domain)
        for domain in ("amazonaws.com", "notion.so", "notion-static.com")
    ):
        return None
    try:
        with requests.get(url, stream=True, timeout=(10, 60), allow_redirects=False) as response:
            if response.status_code != 200:
                return None
            digest = hashlib.sha256()
            size = 0
            for chunk in response.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > max_size:
                    return None
                digest.update(chunk)
            return size, digest.hexdigest()
    except requests.RequestException:
        return None


def walk_payload(blocks):
    for block in blocks:
        yield block
        yield from walk_payload(block.get(block["type"], {}).get("children", []))


def match_unchanged_blocks(old_blocks, old_payload, proposed, resources, canonical, digest_cache):
    comparable = copy.deepcopy(proposed)
    resources_by_id = {
        f"ENEX:{resource.md5}:{resource.file_name}": resource for resource in resources
    }
    required_types = {
        block["type"] for block in walk_payload(proposed)
        if block["type"] in FILE_TYPES and "file_upload" in block[block["type"]]
    }
    verified = {}
    max_size = max((len(resource.data_bin) for resource in resources), default=0)
    for block in walk_payload(old_payload):
        kind = block["type"]
        if kind not in required_types:
            continue
        data = block[kind]
        if data.get("type") != "file":
            continue
        url = data.get("file", {}).get("url")
        if not url:
            continue
        cache_key = (url, max_size)
        if cache_key not in digest_cache:
            digest_cache[cache_key] = hosted_digest(url, max_size)
        digest = digest_cache[cache_key]
        if digest:
            verified.setdefault((kind, *digest), []).append(data)

    for block in walk_payload(comparable):
        kind = block["type"]
        if kind not in FILE_TYPES:
            continue
        data = block[kind]
        resource = resources_by_id.get(data.get("file_upload", {}).get("id"))
        if resource is None:
            continue
        matches = verified.get((kind, len(resource.data_bin), hashlib.sha256(resource.data_bin).hexdigest()), [])
        if matches:
            original = matches.pop(0)
            data.pop("file_upload")
            data["type"] = "file"
            data["file"] = copy.deepcopy(original["file"])
            if "name" in original and "name" not in data:
                data["name"] = original["name"]

    def keys(blocks):
        return [json.dumps(canonical(block), sort_keys=True) for block in blocks]

    matcher = SequenceMatcher(None, keys(old_payload), keys(comparable), autojunk=False)
    retained = {}
    for match in matcher.get_matching_blocks():
        for offset in range(match.size):
            old_index, new_index = match.a + offset, match.b + offset
            if old_index == 0 and new_index > 0:
                continue
            retained[new_index] = old_blocks[old_index]
    return retained