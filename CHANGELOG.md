# new starting point as the previous was a substantially different code-base.

## Unreleased

- Retain unchanged re-import blocks and byte-verified hosted attachments instead of reuploading every file; insert changed blocks around retained content.
- Ignore equivalent date and URL representations and update only changed metadata fields. Redact hosted-file URL query strings from previews.
- Pin the Notion SDK to API version 2022-06-28 so database schema retrieval returns properties consistently with re-import search and queries.
- Pin re-import search to the database-compatible API version; add notebook lookup diagnostics and explicit `--notebook-id` selection.
- Add opt-in `--allow-importer-last-edit` fallback for unknown baselines, checking authenticated integration IDs on pages and all nested blocks.

- Add in-place `--reimport` with notebook-scoped matching and `--whatif N` read-only diffs.
- Add timezone-aware `--skip-edited-after` and baseline-backed `--skip-changed-since-import` safeguards.
- Record successful import baselines and replacement recovery records without modifying Notion database schemas.

- Preserve bulleted and numbered list nesting inside ENEX wrapper elements.
- Upload child blocks instead of dropping nested list content, using batches of
	at most 100 blocks per level and reporting child upload failures as partial imports.
- Preserve email tables, table header cells, task-group contents, and saved embedded clips.
- Handle rich-link styles without trailing semicolons and preserve fallback content when URLs are missing.
- Preserve formatted-text overflow in lists, tasks, headings, code, quotes, callouts, and table first rows.
- Keep table continuation cells within Notion's rich-text limit.
- Avoid extra blank paragraphs from HTML indentation and empty list wrappers, and avoid double-counting
	terminal line-break placeholders. Preserve explicit blank lines and preformatted/code whitespace.