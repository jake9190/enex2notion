# new starting point as the previous was a substantially different code-base.

## Unreleased

- Preserve bulleted and numbered list nesting inside ENEX wrapper elements.
- Upload child blocks instead of dropping nested list content, using batches of
	at most 100 blocks per level and reporting child upload failures as partial imports.