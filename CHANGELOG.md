# new starting point as the previous was a substantially different code-base.

## Unreleased

- Preserve bulleted and numbered list nesting inside ENEX wrapper elements.
- Upload child blocks instead of dropping nested list content, using batches of
	at most 100 blocks per level and reporting child upload failures as partial imports.
- Preserve email tables, table header cells, task-group contents, and saved embedded clips.
- Handle rich-link styles without trailing semicolons and preserve fallback content when URLs are missing.
- Preserve formatted-text overflow in lists, tasks, headings, code, quotes, callouts, and table first rows.
- Keep table continuation cells within Notion's rich-text limit.
- Avoid extra blank paragraphs from HTML indentation and empty list wrappers, and avoid double-counting
	terminal line-break placeholders. Preserve explicit blank lines and preformatted/code whitespace.