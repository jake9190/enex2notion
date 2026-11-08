import argparse
import os
from datetime import datetime
from pathlib import Path

from enex2notion.version import __version__

HELP_ARGS_WIDTH = 29


def _positive_count(value):
    count = int(value)
    if count < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return count


def _aware_datetime(value):
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("use an ISO 8601 date/time with timezone") from error
    if timestamp.tzinfo is None:
        raise argparse.ArgumentTypeError("date/time must include a timezone, such as Z or -04:00")
    return timestamp


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="enex2notion",
        description="Uploads ENEX files to Notion",
        usage="%(prog)s [-h] [--token TOKEN] [OPTION ...] FILE/DIR [FILE/DIR ...]",
        formatter_class=lambda prog: argparse.HelpFormatter(
            prog, max_help_position=HELP_ARGS_WIDTH
        ),
    )

    schema = {
        "enex_input": {
            "type": Path,
            "nargs": "*",  # Make optional for --resolve-links
            "help": "ENEX files or directories to upload",
            "metavar": "FILE/DIR",
        },
        "--resolve-links": {
            "action": "store_true",
            "help": "resolve evernote:// links in imported Notion pages",
        },
        "--check-duplicates": {
            "action": "store_true",
            "help": "scan workspace and create DuplicatePageNames report without resolving links",
        },
        "--retry-failed-links": {
            "action": "store_true",
            "help": "retry failed evernote:// link conversions with enhanced multi-link and parallel processing",
        },
        "--page": {
            "help": (
                "(resolve-links only) analyze only a specific page by name"
                " (useful for testing or fixing individual pages)"
            ),
            "metavar": "NAME",
        },
        "--page-id": {
            "help": "(resolve-links only) analyze only a specific page by exact Notion page ID",
            "metavar": "ID",
        },
        "--page-list": {
            "type": Path,
            "help": (
                "(resolve-links only) path to page list cache file (JSON format)."
                " If file exists, load page map from it instead of scanning Notion."
                " If file doesn't exist, scan Notion and save the page map to this file."
            ),
            "metavar": "FILE",
        },
        "--match-mode": {
            "choices": ["exact", "case-insensitive", "fuzzy"],
            "default": "case-insensitive",
            "help": (
                "(resolve-links only) matching strategy: exact (case-sensitive),"
                " case-insensitive, or fuzzy (similarity-based)"
                " (default: case-insensitive)"
            ),
        },
        "--dry-run": {
            "action": "store_true",
            "help": "(resolve-links only) show matches without updating links",
        },
        "--queue-directory": {
            "type": Path,
            "metavar": "DIR",
            "help": "(resolve-links only) directory to store canonical.json, unfinished.json, completed.json",
        },
        # Deprecated; kept for backward compatibility during transition
        "--queue": {
            "type": Path,
            "metavar": "FILE",
            "help": argparse.SUPPRESS,
        },
        "--limit": {
            "type": int,
            "metavar": "N",
            "help": "(resolve-links/retry-failed-links) process at most N pages from the queue in this run",
        },
        "--workers": {
            "type": int,
            "default": 3,
            "metavar": "N",
            "help": "(resolve-links/retry-failed-links) number of parallel workers for processing pages (default: 3, optimal: 2-3)",
        },
        "--token": {
            "help": (
                "Notion Integration token (create at https://www.notion.com/my-integrations)."
                " Can also use --use-env to read from NOTION_TOKEN environment variable."
                " Example: export NOTION_TOKEN=\"secret_your_token_here\""
                " [NEEDED FOR UPLOAD]"
            ),
        },
        "--use-env": {
            "action": "store_true",
            "help": (
                "use NOTION_TOKEN environment variable for authentication"
                " instead of --token argument"
            ),
        },
        "--root-page": {
            "default": "Evernote ENEX Import",
            "help": (
                "root page name for the imported notebooks,"
                " it will be created if it does not exist"
                ' (default: "Evernote ENEX Import")'
            ),
            "metavar": "NAME",
        },
        "--mode": {
            "choices": ["DB", "PAGE"],
            "default": "DB",
            "help": (
                "upload each ENEX as database (DB) or page with children (PAGE)"
                " (default: DB)"
            ),
        },
        "--add-meta": {
            "action": "store_true",
            "help": (
                "include metadata (created, tags, etc) in notes,"
                " makes sense only with PAGE mode"
            ),
        },
        "--skip-existing": {
            "action": "store_true",
            "help": "skip pages whose title and ENEX updated date match an existing page",
        },
        "--reimport": {
            "action": "store_true",
            "help": "replace previously imported notes in place; skip missing or ambiguous matches",
        },
        "--notebook-id": {
            "metavar": "ID",
            "help": "with --reimport, select one existing notebook by ID within the selected root (one ENEX file only)",
        },
        "--allow-importer-last-edit": {
            "action": "store_true",
            "help": "with --skip-changed-since-import, allow unknown baselines only when the integration last edited the page and all blocks; cannot rule out earlier human edits",
        },
        "--allow-last-editor": {
            "action": "append",
            "default": [],
            "metavar": "NAME",
            "help": "with --skip-changed-since-import, whitelist a last-editor name for unknown baselines (repeatable); names are not unique and earlier edits cannot be ruled out",
        },
        "--allow-initial-edit-window": {
            "type": _positive_count,
            "metavar": "SECONDS",
            "help": "with --skip-changed-since-import, allow unknown baselines when page and nested block edits are within SECONDS of Notion page creation, regardless of editor",
        },
        "--whatif": {
            "type": _positive_count,
            "metavar": "N",
            "help": "with --reimport, preview changes for at most N selected notes without writing",
        },
        "--skip-edited-after": {
            "type": _aware_datetime,
            "metavar": "DATETIME",
            "help": "with --reimport, skip notes edited in Notion after this timezone-qualified ISO date/time",
        },
        "--skip-changed-since-import": {
            "action": "store_true",
            "help": "with --reimport, skip notes changed since the recorded import baseline (or with no baseline)",
        },
        "--tag": {
            "help": "add custom tag to uploaded notes",
        },
        "--condense-lines": {
            "action": "store_true",
            "help": (
                "condense text lines together into paragraphs"
                " to avoid making block per line"
            ),
        },
        "--condense-lines-sparse": {
            "action": "store_true",
            "help": "like --condense-lines but leaves gaps between paragraphs",
        },
        "--operations-dir": {
            "type": Path,
            "metavar": "DIR",
            "help": (
                "base directory for all operational files: done-file, summary, rejected-files, log, "
                "unsupported-files, and infrastructure cache (enex2notion.txt). "
                "If specified, all file paths use this as base unless overridden with absolute paths."
            ),
        },
        "--done-file": {
            "type": Path,
            "metavar": "FILE",
            "help": (
                "file for uploaded notes hashes to resume interrupted upload. "
                "Default: operations-dir/done-{notebook}.txt if --operations-dir specified. "
                "Absolute paths override operations-dir."
            ),
        },
        "--summary": {
            "type": Path,
            "metavar": "FILE",
            "help": (
                "save import summary report to file (always printed to console). "
                "Default: operations-dir/summary.txt if --operations-dir specified. "
                "Absolute paths override operations-dir."
            ),
        },
        "--rejected-files": {
            "type": Path,
            "metavar": "FILE",
            "help": (
                "save rejected/unsupported files report to CSV file. "
                "Default: operations-dir/rejected-files.csv if --operations-dir specified. "
                "Absolute paths override operations-dir."
            ),
        },
        "--unsupported-files": {
            "type": Path,
            "metavar": "DIR",
            "help": (
                "directory to save files that Notion API rejects (organized by notebook/note). "
                "Default: operations-dir/unsupported-files/ if --operations-dir specified. "
                "Absolute paths override operations-dir."
            ),
        },
        "--note": {
            "help": "import only a specific note by exact title from ENEX file(s)",
            "metavar": "TITLE",
        },
        "--note-index": {
            "type": int,
            "help": "import only note at specific index (1-based) from ENEX file",
            "metavar": "N",
        },
        "--log": {
            "type": Path,
            "metavar": "FILE",
            "help": (
                "file to store program log. "
                "Default: operations-dir/enex2notion.log if --operations-dir specified. "
                "Absolute paths override operations-dir."
            ),
        },
        "--verbose": {
            "action": "store_true",
            "help": "output debug information",
        },
        "--version": {
            "action": "version",
            "version": f"%(prog)s {__version__}",  # noqa: WPS323
        },
    }

    for arg, arg_params in schema.items():
        parser.add_argument(arg, **arg_params)

    args = parser.parse_args(argv)

    if (args.whatif or args.skip_edited_after or args.skip_changed_since_import) and not args.reimport:
        parser.error("--whatif and edit safeguards require --reimport")
    if args.skip_existing and args.reimport:
        parser.error("--skip-existing cannot be combined with --reimport")
    if args.notebook_id and not args.reimport:
        parser.error("--notebook-id requires --reimport")
    if args.allow_importer_last_edit and not args.skip_changed_since_import:
        parser.error("--allow-importer-last-edit requires --skip-changed-since-import")
    if args.allow_initial_edit_window is not None and not args.skip_changed_since_import:
        parser.error("--allow-initial-edit-window requires --skip-changed-since-import")
    if args.allow_last_editor and not args.skip_changed_since_import:
        parser.error("--allow-last-editor requires --skip-changed-since-import")
    if any(not name.strip() for name in args.allow_last_editor):
        parser.error("--allow-last-editor requires a nonempty name")
    if args.reimport and (args.resolve_links or args.retry_failed_links or args.check_duplicates):
        parser.error("--reimport cannot be combined with link or duplicate commands")
    if args.reimport and args.dry_run:
        parser.error("use --reimport --whatif N for a read-only preview, not --dry-run")

    # Handle environment variable token
    if args.use_env:
        env_token = os.environ.get("NOTION_TOKEN")
        if not env_token:
            parser.error(
                "--use-env specified but NOTION_TOKEN environment variable is not set.\n"
                "Set it with: export NOTION_TOKEN=\"secret_your_token_here\""
            )
        args.token = env_token
    elif not args.token and "NOTION_TOKEN" in os.environ:
        # Auto-detect if not explicitly specified
        args.token = os.environ["NOTION_TOKEN"]

    if args.reimport and not args.token:
        parser.error("--reimport requires --token or NOTION_TOKEN, including for --whatif")
    
    # Validate arguments based on mode
    if args.resolve_links:
        # Resolve-links mode: token is required, enex_input is not
        if not args.token:
            parser.error(
                "--token or --use-env is required for --resolve-links.\n"
                "Create an Integration token at https://www.notion.com/my-integrations"
            )
        args.command = "resolve-links"
        # Set enex_input to None in resolve-links mode (not used)
        args.enex_input = None
    elif args.retry_failed_links:
        # Retry-failed-links mode: token is required, enex_input is not
        if not args.token:
            parser.error(
                "--token or --use-env is required for --retry-failed-links.\n"
                "Create an Integration token at https://www.notion.com/my-integrations"
            )
        args.command = "retry-failed-links"
        # Set enex_input to None (not used)
        args.enex_input = None
    elif args.check_duplicates:
        # Check-duplicates mode: token is required, enex_input is not
        if not args.token:
            parser.error(
                "--token or --use-env is required for --check-duplicates.\n"
                "Create an Integration token at https://www.notion.com/my-integrations"
            )
        args.command = "check-duplicates"
        # Set enex_input to None (not used)
        args.enex_input = None
    else:
        # Upload mode: enex_input is required
        if not args.enex_input:
            parser.error("the following arguments are required: FILE/DIR")
        args.command = "upload"
    
    return args
