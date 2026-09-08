import copy
import difflib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from enex2notion.cli_notion import get_notion_wrapper
from enex2notion.enex_parser import parse_all_notes
from enex2notion.enex_uploader import _convert_note_blocks, _process_image_blocks, _upload_block_tree
from enex2notion.import_state import (
    ImportRemote, ImportState, belongs_to, edit_signature, latest_edit, object_title, same_id, write_json,
)
from enex2notion.note_parser.note import parse_note
from enex2notion.notion_api_wrapper import note_to_database_properties
from enex2notion.notion_blocks.uploadable import NotionUploadableBlock
from enex2notion.reimport_attachments import FILE_TYPES, match_unchanged_blocks, walk_payload
from enex2notion.utils_static import Rules


def comparison_url(url):
    if not isinstance(url, str):
        return url
    unreserved = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"

    def normalize(component, allowed):
        return re.sub(
            r"%[0-9a-fA-F]{2}",
            lambda match: chr(int(match[0][1:], 16)) if chr(int(match[0][1:], 16)) in allowed else match[0].upper(),
            component,
        )

    try:
        parts = urlsplit(url)
        return urlunsplit((
            parts.scheme, parts.netloc, normalize(parts.path, unreserved),
            normalize(parts.query, unreserved), normalize(parts.fragment, unreserved + "'"),
        ))
    except ValueError:
        return url


def comparison_date(value):
    result = dict(value)
    for key in ("start", "end"):
        text = result.get(key)
        if not isinstance(text, str) or "T" not in text:
            continue
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo:
                result[key] = parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    return result


def canonical(value):
    if isinstance(value, list):
        return [canonical(item) for item in value]
    if not isinstance(value, dict):
        return value
    ignored = {
        "object", "id", "created_time", "last_edited_time", "created_by", "last_edited_by",
        "has_children", "archived", "in_trash", "plain_text", "href", "expiry_time",
    }
    result = {}
    for key, item in value.items():
        if key in ignored:
            continue
        if key == "file_upload":
            result[key] = item
            continue
        if key == "url":
            item = comparison_url(item)
        elif key == "date" and isinstance(item, dict):
            item = comparison_date(item)
        elif key == "rich_text" and isinstance(item, list):
            item = [
                segment for segment in item
                if not (
                    isinstance(segment, dict) and segment.get("type") == "text"
                    and segment.get("text", {}).get("content") == ""
                )
            ]
        cleaned = canonical(item)
        if cleaned in (None, [], {}) or (key == "color" and cleaned == "default"):
            continue
        if key in {"bold", "italic", "strikethrough", "underline", "code", "is_toggleable"} and cleaned is False:
            continue
        result[key] = cleaned
    return result


def old_payload(blocks):
    result = []
    for block in blocks:
        block_type = block["type"]
        data = copy.deepcopy(block.get(block_type, {}))
        if block.get("_children"):
            data["children"] = old_payload(block["_children"])
        result.append({"type": block_type, block_type: data})
    return result


def display_payload(value):
    if isinstance(value, list):
        return [display_payload(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: display_payload(item) for key, item in value.items()}
    file_data = result.get("file")
    if isinstance(file_data, dict) and isinstance(file_data.get("url"), str):
        try:
            parts = urlsplit(file_data["url"])
            file_data["url"] = urlunsplit(parts._replace(query="", fragment=""))
        except ValueError:
            file_data["url"] = "[unavailable hosted file URL]"
    return result


def proposed_properties(note, schema):
    if schema is None:
        return {"title": {"title": [{"type": "text", "text": {"content": note.title}}]}}
    owned = {
        name: definition for name, definition in schema.items()
        if "title" in definition or definition.get("type") == "title"
        or name in {"Created", "Updated", "URL", "Tags", "Partial Import"}
    }
    properties = note_to_database_properties(note, owned)
    if "URL" in owned and ("url" in owned["URL"] or owned["URL"].get("type") == "url"):
        properties["URL"] = {"url": note.url or None}
    if "Tags" in owned and ("multi_select" in owned["Tags"] or owned["Tags"].get("type") == "multi_select"):
        properties["Tags"] = {"multi_select": [{"name": tag} for tag in note.tags]}
    return properties


def property_projection(page, properties):
    return {
        name: {kind: page.get("properties", {}).get(name, {}).get(kind)}
        for name, definition in properties.items() for kind in definition
    }


def preview_blocks(blocks):
    prepared = copy.deepcopy(blocks)

    def prepare(items):
        for block in items:
            if isinstance(block, NotionUploadableBlock):
                if not block.resource:
                    raise ValueError("Attachment has no ENEX resource")
                block.attrs["file_upload_id"] = f"ENEX:{block.resource.md5}:{block.resource.file_name}"
            prepare(block.children)

    prepare(prepared)
    return _convert_note_blocks(prepared)


def all_last_edited_by(snapshot, user_id):
    def matches(value):
        editor_id = value.get("last_edited_by", {}).get("id")
        return bool(editor_id) and same_id(editor_id, user_id) and all(
            matches(child) for child in value.get("_children", [])
        )

    return bool(user_id) and matches(snapshot["page"]) and all(matches(block) for block in snapshot["blocks"])


def protection_reason(snapshot, record, cutoff, skip_changed, importer_id=None):
    if cutoff and latest_edit(snapshot) > cutoff:
        return "edited in Notion after the cutoff"
    if skip_changed:
        if not record or not same_id(record["page_id"], snapshot["page"]["id"]):
            if importer_id and all_last_edited_by(snapshot, importer_id):
                return None
            return "no recorded import baseline; cannot establish whether Notion was edited"
        if record["signature"] != edit_signature(snapshot):
            return "changed in Notion since the recorded import"
    return None


class Reimporter:
    def __init__(self, wrapper, args, directory, output=print):
        self.wrapper = wrapper
        self.remote = ImportRemote(wrapper)
        self.args = args
        self.rules = Rules.from_args(args)
        self.directory = Path(directory)
        self.state = ImportState(directory)
        self.output = output
        self.importer_id = None
        self.attachment_digests = {}

    def plan(self, note, notebook_id, pages, schema):
        record = self.state.get(notebook_id, note)
        active = [page for page in pages if not page.get("archived") and not page.get("in_trash")]
        if record:
            candidates = [page for page in active if same_id(page["id"], record["page_id"])]
        else:
            candidates = [page for page in active if object_title(page) == note.title]
        candidates = [page for page in candidates if belongs_to(page, notebook_id)]
        if len(candidates) != 1:
            return {"action": "skip", "reason": "missing or ambiguous existing note match"}
        for backup in (self.directory / "reimport-backups").glob(f"{candidates[0]['id']}-*.json"):
            if json.loads(backup.read_text(encoding="utf-8")).get("status") != "complete":
                return {"action": "skip", "reason": f"unresolved recovery record: {backup}"}
        try:
            snapshot = self.remote.snapshot(candidates[0]["id"])
        except ValueError as error:
            return {"action": "skip", "reason": str(error)}
        if not belongs_to(snapshot["page"], notebook_id):
            return {"action": "skip", "reason": "page moved out of the selected notebook"}
        editor_fallback = self.args.allow_importer_last_edit and not record
        if editor_fallback and self.importer_id is None:
            identity = self.remote.call(self.wrapper.client.users.me)
            if identity.get("type") != "bot" or not identity.get("id"):
                return {"action": "skip", "reason": "could not verify authenticated integration user ID"}
            self.importer_id = identity["id"]
        reason = protection_reason(
            snapshot, record, self.args.skip_edited_after, self.args.skip_changed_since_import,
            self.importer_id if editor_fallback else None,
        )
        if reason:
            return {"action": "skip", "reason": reason}

        def has_protected_block(blocks):
            return any(
                block["type"] in {"child_page", "child_database", "synced_block", "unsupported"}
                or has_protected_block(block.get("_children", [])) for block in blocks
            )

        if has_protected_block(snapshot["blocks"]):
            return {"action": "skip", "reason": "page contains child pages, databases, synced or unsupported blocks"}
        note = copy.deepcopy(note)
        if self.rules.tag and self.rules.tag not in note.tags:
            note.tags.append(self.rules.tag)
        blocks, errors = parse_note(note, self.rules)
        if errors or not blocks:
            return {"action": "skip", "reason": "ENEX conversion is empty or has errors: " + "; ".join(errors)}
        proposed = preview_blocks(blocks)
        if not proposed:
            return {"action": "skip", "reason": "ENEX conversion produced no uploadable blocks"}
        properties = proposed_properties(note, schema)
        original_payload = old_payload(snapshot["blocks"])
        retained = match_unchanged_blocks(
            snapshot["blocks"], original_payload, proposed, note.resources, canonical, self.attachment_digests,
        )
        compared_proposed = [
            old_payload([retained[index]])[0] if index in retained else block
            for index, block in enumerate(proposed)
        ]
        original_properties = property_projection(snapshot["page"], properties)
        property_changes = {
            name: value for name, value in properties.items()
            if canonical(value) != canonical(original_properties.get(name))
        }
        before = canonical({"blocks": original_payload, "properties": original_properties})
        after = canonical({"blocks": compared_proposed, "properties": properties})
        return {
            "action": "unchanged" if before == after else "replace",
            "editor_fallback": editor_fallback,
            "snapshot": snapshot, "blocks": blocks, "properties": property_changes,
            "retained": retained, "proposed": proposed,
            "before": before, "after": after, "note": note, "notebook_id": notebook_id,
        }

    def display(self, title, plan):
        self.output(f"{plan['action'].upper()}: {title}" + (f" ({plan['reason']})" if plan.get("reason") else ""))
        if plan.get("editor_fallback"):
            self.output("  Allowed by last-editor fallback: the integration last edited the page and all blocks; earlier human edits cannot be ruled out.")
        if plan["action"] != "replace":
            return
        kept = len(plan["retained"])
        files_kept = sum(
            block["type"] in FILE_TYPES
            for root in plan["retained"].values() for block in walk_payload(old_payload([root]))
        )
        files_uploaded = sum(
            block["type"] in FILE_TYPES and "file_upload" in block[block["type"]]
            for index, root in enumerate(plan["proposed"]) if index not in plan["retained"]
            for block in walk_payload([root])
        )
        self.output(
            f"  Page: {plan['snapshot']['page']['id']}; keep {kept}, "
            f"archive {len(plan['snapshot']['blocks']) - kept}, "
            f"insert {len(plan['proposed']) - kept} top-level blocks; "
            f"keep {files_kept} file/image blocks, upload {files_uploaded} attachments"
        )
        self.output("  ENEX file_upload references are planned attachment uploads, not Notion upload IDs.")
        self.output("  Hosted-file URL query strings are omitted from this diff.")
        for line in difflib.unified_diff(
            json.dumps(display_payload(plan["before"]), indent=2, ensure_ascii=True).splitlines(),
            json.dumps(display_payload(plan["after"]), indent=2, ensure_ascii=True).splitlines(),
            fromfile="Notion", tofile="ENEX replacement", lineterm="",
        ):
            self.output(line)

    def execute(self, plan):
        if self.args.whatif:
            raise RuntimeError("Preview cannot execute updates")
        snapshot = plan["snapshot"]
        page_id = snapshot["page"]["id"]
        original_ids = {block["id"].replace("-", "").lower() for block in snapshot["blocks"]}
        if edit_signature(self.remote.snapshot(page_id)) != edit_signature(snapshot):
            raise RuntimeError("Page changed after planning; no content replaced")
        journal_path = self.directory / "reimport-backups" / f"{page_id}-{uuid4().hex}.json"
        journal = {"status": "prepared", "original": snapshot, "new_root_ids": [], "archived_ids": []}
        retained_ids = {block["id"] for block in plan["retained"].values()}
        journal["retained_ids"] = sorted(retained_ids)
        write_json(journal_path, journal)
        blocks = []
        source_ranges = []
        position = 0
        for source in plan["blocks"]:
            count = len(preview_blocks([source]))
            indices = list(range(position, position + count))
            if any(index not in plan["retained"] for index in indices):
                blocks.append(copy.deepcopy(source))
                source_ranges.append(indices)
            position += count
        warnings = _process_image_blocks(blocks, self.wrapper, notebook_name="reimport", note_title=plan["note"].title)
        if warnings:
            raise RuntimeError("Attachment upload warnings; original note preserved: " + "; ".join(warnings))

        def validate_files(items):
            for block in items:
                if isinstance(block, NotionUploadableBlock) and not block.attrs.get("file_upload_id"):
                    raise RuntimeError("Attachment did not upload; original note preserved")
                validate_files(block.children)

        validate_files(blocks)
        additions = {}
        for source, indices in zip(blocks, source_ranges):
            converted = _convert_note_blocks([source])
            if len(converted) != len(indices):
                raise RuntimeError("Conversion changed after planning; original note preserved")
            additions.update({index: block for index, block in zip(indices, converted) if index not in plan["retained"]})
        if edit_signature(self.remote.snapshot(page_id)) != edit_signature(snapshot):
            raise RuntimeError("Page changed during attachment preparation; no content replaced")
        new_roots = []
        try:
            pending = []
            anchor = snapshot["blocks"][0]["id"] if retained_ids and snapshot["blocks"] else None

            def insert_pending():
                nonlocal anchor
                if pending:
                    uploaded = _upload_block_tree(
                        self.wrapper, page_id, pending, uploaded_roots=new_roots, after=anchor,
                    )
                    anchor = uploaded[-1]["id"]
                    pending.clear()

            for index in range(len(plan["proposed"])):
                if index in plan["retained"]:
                    insert_pending()
                    anchor = plan["retained"][index]["id"]
                else:
                    pending.append(additions[index])
            insert_pending()
            journal["new_root_ids"] = [block["id"] for block in new_roots]
            if any(block_id.replace("-", "").lower() in original_ids for block_id in journal["new_root_ids"]):
                raise RuntimeError("Append response included original block IDs; original blocks were not archived")
            journal["status"] = "replacement_uploaded"
            write_json(journal_path, journal)
            current = self.remote.snapshot(page_id)
            new_ids = set(journal["new_root_ids"])
            remaining = [block for block in current["blocks"] if block["id"] not in new_ids]
            if edit_signature({"page": snapshot["page"], "blocks": remaining}) != edit_signature(snapshot):
                raise RuntimeError("Original content changed during upload; old blocks were not archived")
            for field in ("properties", "icon", "cover", "parent"):
                if canonical(current["page"].get(field)) != canonical(snapshot["page"].get(field)):
                    raise RuntimeError("Page metadata changed during upload; old blocks were not archived")
            for block in snapshot["blocks"]:
                if block["id"] in retained_ids:
                    continue
                self.remote.archive(block["id"])
                journal["archived_ids"].append(block["id"])
                write_json(journal_path, journal)
            if plan["properties"]:
                self.remote.update_properties(page_id, plan["properties"])
            self.state.record(plan["notebook_id"], plan["note"], self.remote.snapshot(page_id))
            journal["status"] = "complete"
            write_json(journal_path, journal)
        except Exception as error:
            journal["status"] = "incomplete"
            journal["error"] = str(error)
            journal["new_root_ids"] = [
                block["id"] for block in new_roots
                if block["id"].replace("-", "").lower() not in original_ids
            ]
            write_json(journal_path, journal)
            raise RuntimeError(f"Re-import interrupted; stop and review recovery record {journal_path}: {error}") from error


def state_directory(args):
    if args.operations_dir:
        return args.operations_dir
    if args.done_file and args.done_file.is_absolute():
        return args.done_file.parent
    if args.unsupported_files and args.unsupported_files.is_absolute():
        return args.unsupported_files.parent
    return Path.cwd()


def reimport_command(args):
    directory = state_directory(args)
    wrapper = get_notion_wrapper(args.token)
    importer = Reimporter(wrapper, args, directory)
    if args.root_page in {"$WORKSPACE", "WORKSPACE", "__WORKSPACE__"}:
        root_id = None
    else:
        roots = [item for item in importer.remote.search(args.root_page) if item.get("object") == "page"]
        if len(roots) != 1:
            raise ValueError("Re-import requires one unambiguous existing root page")
        root_id = roots[0]["id"]
    files = []
    for path in args.enex_input:
        files.extend(sorted(path.glob("**/*.enex")) if path.is_dir() else [path])
    files = list(dict.fromkeys(path.resolve() for path in files))
    if args.notebook_id and len(files) != 1:
        raise ValueError("--notebook-id requires exactly one ENEX file")
    visited = 0
    totals = Counter()
    for path in files:
        if args.whatif and visited >= args.whatif:
            break
        stats = parse_all_notes(path, note_title_filter=args.note, note_index_filter=args.note_index)
        notes = [result.note for result in stats.results if not result.failed and result.note]
        titles = Counter(note.title for note in notes)
        expected_type = "database" if args.mode == "DB" else "page"
        if args.notebook_id:
            selected = wrapper.get_database(args.notebook_id) if args.mode == "DB" else importer.remote.page(args.notebook_id)
            candidates = [selected]
        else:
            candidates = importer.remote.search(path.stem)
        notebooks = [item for item in candidates
                     if item.get("object") == expected_type and belongs_to(item, root_id)
                     and not item.get("archived") and not item.get("in_trash")]
        notebook_failure = (
            f"notebook lookup for '{path.stem}' (--mode {args.mode}, root {root_id}): "
            f"{len(notebooks)} matching notebooks; "
            "check filename, --mode, root access, or supply --notebook-id"
        )
        if len(notebooks) != 1:
            importer.output(notebook_failure)
            for candidate in candidates:
                importer.output(
                    f"  Candidate: {candidate.get('id')} type={candidate.get('object')} "
                    f"title={object_title(candidate)!r} parent={candidate.get('parent')}"
                )
        pages, schema = [], None
        if len(notebooks) == 1:
            notebook_id = notebooks[0]["id"]
            pages = importer.remote.notebook_pages(notebook_id, args.mode)
            if args.mode == "DB":
                schema = wrapper.get_database(notebook_id)["properties"]
        for result in stats.results:
            if args.whatif and visited >= args.whatif:
                break
            visited += 1
            note = result.note
            if result.failed or not note:
                plan = {"action": "skip", "reason": "ENEX note could not be parsed"}
            elif len(notebooks) != 1:
                plan = {"action": "skip", "reason": notebook_failure}
            elif titles[note.title] > 1:
                plan = {"action": "skip", "reason": "duplicate source title; cannot safely match"}
            else:
                plan = importer.plan(note, notebook_id, pages, schema)
            importer.display(note.title if note else "Unparseable note", plan)
            totals[plan["action"]] += 1
            if plan["action"] == "replace" and not args.whatif:
                importer.execute(plan)
    print(f"{'WHATIF' if args.whatif else 'REIMPORT'}: {visited} notes examined; "
          f"{totals['replace']} {'would replace' if args.whatif else 'replaced'}, "
          f"{totals['unchanged']} unchanged, {totals['skip']} skipped")