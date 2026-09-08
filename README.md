# enex2notion

[![PyPI version](https://img.shields.io/pypi/v/enex2notion?label=version)](https://pypi.python.org/pypi/enex2notion)
[![Python Version](https://img.shields.io/pypi/pyversions/enex2notion.svg)](https://pypi.org/project/enex2notion/)
[![tests](https://github.com/vzhd1701/enex2notion/actions/workflows/test.yml/badge.svg)](https://github.com/vzhd1701/enex2notion/actions/workflows/test.yml)
[![codecov](https://codecov.io/gh/vzhd1701/enex2notion/branch/master/graph/badge.svg)](https://codecov.io/gh/vzhd1701/enex2notion)

Easy way to import [Evernote's](https://www.evernote.com/) `*.enex` files to [Notion.so](https://notion.so)


## Quick Start

```bash
# Clone and install
git clone https://github.com/CameronBeeler/enex2notion.git
cd enex2notion/
pip install -r requirements.txt

# Set up Notion Integration token (see below)
export NOTION_TOKEN="secret_your_integration_token_here"

# Test (dry run - no upload)
python -m enex2notion your_notebook.enex --verbose

# Upload to Notion
python -m enex2notion your_notebook.enex --use-env --summary summary.txt
```

### What is preserved

- Embedded files and images are uploaded to Notion
  - nested images will appear after paragraph
- Text formatting (**bold**, _italic_, etc) and colors
- Tables are converted to the new format (no colspans though)
- Tasks
- Web Clips
  - as plain text or PDFs, see [below](#web-clips)
- Everything else basically

### What is lost

- Paragraph alignment
- Subscript and superscript formatting
- Custom fonts and font sizes
- Encrypted blocks
  - just decrypt them before export

## Installation

If you are not familiar with command line programs, take a look at these step-by-step guides:

### [Step-by-step guide for Windows](https://vzhd1701.notion.site/How-to-use-enex2notion-on-Windows-6fa980b489ab4414a5317e631e7f6bc6)

### [Step-by-step guide for macOS](https://vzhd1701.notion.site/How-to-use-enex2notion-on-macOS-a912dd63e3d14da886a413d3f83efb67)

### Using portable binary

[**Download the latest binary release**](https://github.com/vzhd1701/enex2notion/releases/latest) for your OS.

### With [Homebrew](https://brew.sh/) (Recommended for macOS)

```bash
$ brew install enex2notion
```

### With [**PIPX**](https://github.com/pipxproject/pipx) (Recommended for Linux & Windows)

```shell
$ pipx install enex2notion
```

### With [**Docker**](https://docs.docker.com/)

[![Docker Image Size (amd64)](<https://img.shields.io/docker/image-size/vzhd1701/enex2notion?arch=amd64&label=image%20size%20(amd64)>)](https://hub.docker.com/r/vzhd1701/enex2notion)
[![Docker Image Size (arm64)](<https://img.shields.io/docker/image-size/vzhd1701/enex2notion?arch=arm64&label=image%20size%20(arm64)>)](https://hub.docker.com/r/vzhd1701/enex2notion)

This command maps current directory `$PWD` to the `/input` directory in the container. You can replace `$PWD` with a directory that contains your `*.enex` files. When running commands like `enex2notion /input` refer to your local mapped directory as `/input`.

```shell
$ docker run --rm -t -v "$PWD":/input vzhd1701/enex2notion:latest
```

### With PIP

```bash
$ pip install --user enex2notion
```

**Python 3.12 or later required.**

### From source

This project uses standard Python packaging tools.

```shell
$ git clone https://github.com/CameronBeeler/enex2notion.git
$ cd enex2notion/
$ pip install -r requirements.txt
$ python -m enex2notion
```

**Or install in development mode:**

```shell
$ pip install -e .
$ enex2notion
```

## Usage

### Re-import Existing Notes

Re-import updates note content in the **same Notion page**, retaining its page ID
and page links. It does not merge edits. Unchanged top-level blocks (including
their unchanged children) stay in place. Changed blocks are archived and replaced,
so links and comments on replaced blocks are not transferred; retained blocks
keep their IDs.

Start with a read-only preview of at most five selected notes:

```shell
python -m enex2notion Notebook.enex --use-env --reimport --whatif 5 --operations-dir ./operations
```

The preview shows per-note skip reasons and before/after JSON diffs for blocks,
formatting, nesting, and imported metadata. The limit is global across input files
and counts skipped and unchanged notes too. `--note TITLE` and `--note-index N`
still filter the source notes. Preview requires Notion read access but makes no
Notion writes, attachment uploads, log writes, or operational-file changes.
To verify attachments, preview and real updates may download current Notion-hosted
files and compare their size and SHA-256 digest with the ENEX bytes. Matching
filenames alone are not trusted. Downloads are size-bounded, do not forward the
integration token, and do not follow redirects. Verified unchanged file/photo
blocks remain in Notion without reuploading, including those inside unchanged
top-level subtrees. The preview reports how many blocks and attachments it will
keep, archive, insert, or upload. Temporary hosted-file URL query strings are
omitted from displayed diffs.

Files in changed parent subtrees, files that need repositioning which cannot be
achieved around retained blocks, and files whose bytes cannot be verified may
still need uploading. Expired/inaccessible URLs or transformed image bytes can
prevent verification. Preview shows these as `file_upload` placeholders identifying
the ENEX hash and filename; actual uploads remain subject to Notion validation.

Equivalent timezone/millisecond date representations and equivalent URL escapes
are ignored when comparing values. Only changed metadata fields are written.
Real date differences are preserved: `21:32:00` and `21:32:10` differ by ten seconds
and are not treated as equivalent. Reserved URL separators are not decoded.

Apply replacements, protecting notes edited after a cutoff:

```shell
python -m enex2notion Notebook.enex --use-env --reimport --skip-edited-after "2026-09-01T00:00:00Z" --operations-dir ./operations
```

Use an ISO 8601 timestamp with a timezone (`Z` or an offset such as `-04:00`).
Notes whose page or nested blocks have a later edit timestamp are skipped;
an edit exactly at the cutoff is allowed. Edits by integrations count too.

For notes with a recorded import baseline:

```shell
python -m enex2notion Notebook.enex --use-env --reimport --skip-changed-since-import --operations-dir ./operations
```

Successful new imports and re-imports now save page mappings and page/block edit
timestamps in `import-state.json`. Keep using the same operations directory.
Without `--operations-dir`, both import paths use the parent of an absolute
`--done-file`, then the parent of an absolute `--unsupported-files`, otherwise the
current directory. The since-import flag skips notes with no known
baseline, including older imports: Notion's current edit time alone cannot prove
whether a note has changed since import. Both protection flags can be combined.

For older imports without a baseline, an optional last-editor fallback is available:

```shell
python -m enex2notion Notebook.enex --use-env --reimport --whatif 5 --skip-changed-since-import --allow-importer-last-edit --operations-dir ./operations
```

This requires the page and **every nested block** to have `last_edited_by.id`
equal to the authenticated integration's user ID, not merely the display name
`enex2notion`. Missing or different editors are rejected. It applies only without
a recorded baseline and never overrides a changed baseline or date cutoff.
This is a heuristic: an integration edit can follow an earlier human edit, so it
does not prove the absence of human changes. Review the preview before applying.

`missing or ambiguous notebook` is a discovery problem, not an edit-protection
decision. Re-import now pins search to the same database-compatible Notion API
version used for database queries. Lookup failures report the expected filename
stem, mode, root, and candidate IDs. For a renamed export or duplicate notebook
names, add `--notebook-id ID` with exactly one ENEX file; the specified notebook
must still belong to the selected root and match `--mode DB` or `--mode PAGE`.

Matching is restricted to the existing root and notebook (the ENEX filename without
its extension), in the selected `--mode DB` or `--mode PAGE`. A recorded page ID
takes precedence; older imports require a unique exact title match. Missing or
ambiguous notebooks/pages, duplicate source titles, empty/failed conversions, and
pages containing child pages/databases or synced/unsupported blocks are skipped.
Re-import does not create missing notes or use done-file hashes to skip updates.
Keep source titles and creation timestamps stable to reuse recorded identities.
Unrelated database properties, page icons, and covers are left alone; standard
imported title, Created, Updated, URL, Tags, and Partial Import fields are updated
only when their values differ.

Before replacing content, the importer writes a JSON recovery record under
`operations/reimport-backups/`, uploads new blocks, rechecks the original content,
then archives only the replaced blocks. Retained block IDs are included in the
record. Failed requests stop the run and leave an incomplete
record that blocks automatic retries for that page. These records contain note
content and should be protected like the ENEX files. They are recovery records,
not self-contained attachment backups; Notion-hosted file URLs may expire.

Empty rich-text segments compare equal to an empty rich-text array; intentional
blank paragraphs are retained, not deleted. An unexpected append-response count
still stops the import before archiving original blocks. The error includes the
expected and returned counts, and returned root IDs are recorded for inspection.
Older versions did not record IDs from a mismatched batch, so an empty
`new_root_ids` list does not prove that the failed request created nothing.

Anchored inserts read existing child IDs with pagination before writing, so
append responses containing existing siblings are filtered to new blocks only.
Omitted heading `is_toggleable` and explicit `false` compare equally; `true`
remains a meaningful difference.

Some older recovery records may include original sibling IDs in `new_root_ids`.
Always compare that list with `original.blocks` and exclude all original IDs
before considering any block for removal. Do not bulk-delete blocks based only
on `new_root_ids`. Current records exclude original root IDs on failures.

For an interrupted replacement, inspect its original block IDs, newly created
root IDs, and archived IDs. Restore the original blocks (unarchive if necessary)
and remove the added blocks in Notion before moving the incomplete record out of
the backup directory. An interrupted response can leave blocks whose IDs were
not returned, so inspect the page as well as the record. Do not simply rerun over
an incomplete page. Notion has no atomic replacement or conditional-write API;
avoid editing pages during a re-import, and do not run concurrent import processes
against the same pages or state directory.

```shell
$ enex2notion --help
usage: enex2notion [-h] [--token TOKEN] [OPTION ...] FILE/DIR [FILE/DIR ...]

Uploads ENEX files to Notion

positional arguments:
  FILE/DIR                   ENEX files or directories to upload

options:
  -h, --help                 show this help message and exit
  --token TOKEN              Notion Integration token (create at https://www.notion.com/my-integrations). Can also use --use-env to read from NOTION_TOKEN environment variable.
                             Example: export NOTION_TOKEN="secret_your_token_here" [NEEDED FOR UPLOAD]
  --use-env                  use NOTION_TOKEN environment variable for authentication instead of --token argument
  --root-page NAME           root page name for the imported notebooks, it will be created if it does not exist (default: "Evernote ENEX Import")
  --mode {DB,PAGE}           upload each ENEX as database (DB) or page with children (PAGE) (default: DB)
  --add-meta                 include metadata (created, tags, etc) in notes, makes sense only with PAGE mode
  --tag TAG                  add custom tag to uploaded notes
  --condense-lines           condense text lines together into paragraphs to avoid making block per line
  --condense-lines-sparse    like --condense-lines but leaves gaps between paragraphs
  --done-file FILE           file for uploaded notes hashes to resume interrupted upload
  --summary FILE             save import summary report to file (always printed to console)
  --rejected-files FILE      save rejected/unsupported files report to CSV file
  --unsupported-files DIR    directory to save files that failed to upload (manual upload needed)
  --operations-dir DIR       directory for operational files (done-file, summary, rejected-files, logs, cache)
                             When specified, relative paths for --done-file, --summary, --rejected-files, --log,
                             and --unsupported-files are resolved relative to this directory
  --log FILE                 file to store program log
  --verbose                  output debug information
  --version                  show program's version number and exit
```

### Input

You can pass single `*.enex` files or directories. The program will recursively scan directories for `*.enex` files.

### Integration Token & Authentication

The upload requires a Notion Integration token. To set one up:

1. Go to https://www.notion.com/my-integrations
2. Click "+ New integration"
3. Give it a name (e.g., "enex2notion") and submit
4. Copy the Integration token (starts with `secret_`)
5. Create a page in Notion to import into
6. Share that page with your Integration (via "..." menu → "Add connections")

**Using Environment Variable (Recommended)**:

```bash
# For current session
export NOTION_TOKEN="secret_your_integration_token_here"

# To persist across sessions, add to shell profile:
# For bash: add to ~/.bashrc or ~/.bash_profile
# For zsh: add to ~/.zshrc
echo 'export NOTION_TOKEN="secret_your_integration_token_here"' >> ~/.zshrc
source ~/.zshrc

# Use with --use-env flag
python -m enex2notion notebook.enex --use-env
```

**Or pass token directly**:

```bash
python -m enex2notion notebook.enex --token secret_your_token_here
```

**Dry Run Mode**: The program can run without authentication. It will not make any network requests without a token. Executing a dry run with `--verbose` is an excellent way to check if your `*.enex` files are parsed correctly before uploading.

### Upload continuation

The upload will take some time since each note is uploaded block-by-block, so you'll probably need some way of resuming it. `--done-file` is precisely for that. All uploaded note hashes will be stored there, so the next time you start, the upload will continue from where you left off.

All uploaded notebooks will appear under the automatically created `Evernote ENEX Import` page. You can change that name with the `--root-page` option. The program will mark unfinished notes with `[UNFINISHED UPLOAD]` text in the title. After successful upload, the mark will be removed.

### Summary Reports & Partial Imports

After import completion, a detailed summary report is displayed showing:
- Total notes per notebook and overall
- Success/failure/skip rates with percentages
- Processing time

#### Partial Import System

**Every Evernote note creates a Notion page**, even if import errors occur. Notes with import errors are marked as "partial imports" with:

1. **Inline error callout** at the top of the page listing what failed
2. **Source URL bookmark** (for web clips) so you can access the original
3. **"Partial Import" checkbox** set to `true` in database mode
4. **Exception tracking** - An "Exceptions" page is created under your root page with links to all partially imported notes organized by notebook

**Setting up Exception View (DB mode only)**:

To easily filter partial imports in your database:

1. Open the database in Notion
2. Click "+ New view" → "Table"
3. Name it "Exceptions"
4. Add filter: "Partial Import" → "is" → "Checked"
5. (Optional) On default Table view: hover over Name column footer → "Calculate" → "Count All"

**Note**: Database views must be created manually in Notion - the API does not support programmatic view creation.

Use `--summary FILE` to save the import report to a file.

### Operations Directory & File Organization

For complex migrations with multiple notebooks, use `--operations-dir` to centralize all operational files:

```bash
python -m enex2notion --use-env --mode DB --add-meta --condense-lines-sparse \
  --operations-dir ~/Downloads/imports/output \
  --unsupported-files ~/Downloads/imports/NotionDocuments/ \
  --done-file done-<notebook-name>.txt \
  --summary summary-<notebook-name>.txt \
  --rejected-files rejected-<notebook-name>.txt \
  ~/Downloads/imports/Notebooks/<notebook-name>.enex
```

**Directory Structure**:

```
~/Downloads/imports/
├── Notebooks/              # Input: Your .enex files
│   └── <notebook-name>.enex    # the enex file exported from Evernote
├── output/                 # Operations directory (--operations-dir)
│   ├── enex2notion.txt    # Infrastructure cache (auto-created)
│   ├── done-<notebook-name>.txt 
│   ├── summary-<notebook-name>.txt 
│   ├── rejected-<notebook-name>.csv  # Rejected files report
│   └── enex2notion.log    # (if --log specified)
└── NotionDocuments/       # Failed uploads (--unsupported-files)
    └── <notebook-name> /   # Per-notebook subdirectories
        ├── document1.pdf
        └── image2.png
```

**Key Files**:

- **`enex2notion.txt`** (Infrastructure Cache)
  - Auto-created in operations directory
  - Stores IDs for "Exceptions" page and "User Action Required" database
  - Shared across all imports to reuse infrastructure
  - Tab-delimited format: `name<TAB>id`

- **`done-*.txt`** (Progress Files)
  - Tracks uploaded note hashes for resume capability
  - Also stores database IDs for each notebook
  - One file per notebook for parallel imports

- **`summary-*.txt`** (Import Reports)
  - Detailed statistics: total notes, success/failure rates, processing time
  - Lists directories for failed uploads
  - One file per notebook

- **`rejected-*.csv`** (Rejected Files Report)
  - Lists files that couldn't be uploaded (unsupported types, banned extensions)
  - Includes notebook name, note title, filename, and reason

- **Failed Upload Directories**
  - Format: `<notebook-name>_<timestamp>_failed/`
  - Contains files that failed to upload to Notion
  - Referenced in "User Action Required" database for manual upload

**Benefits**:
- All operational files in one location for easy management
- Shared infrastructure cache prevents duplicate databases
- Clean separation between input (.enex), operations (logs/tracking), and output (failed files)
- Supports parallel imports of multiple notebooks

### Exception Tracking & User Action Required Database

Every import automatically creates a maintenance system in Notion:

**Structure in Notion UI**:
```
Evernote ENEX Import/          # Root page (--root-page)
├── Exceptions/                # Auto-created summary page
│   ├── User Action Required   # Database for actionable items
│   └── <enex-notebook-name>   # one to many Pages that will include all non-actionable issues with the import of this note
├── <notebook-1-name> /                  # Your imported notebooks
├── <notebook-2-name> /
└── <etc-etc-notebook-name> /
```

**User Action Required Database**:

Tracks items requiring manual intervention with these properties:

| Property | Type | Description |
|----------|------|-------------| 
| Title | Title | Auto-generated: `<note-title>-<error-type>-<count>` |
| Import Source | Text | Source ENEX filename |
| Notion Source Page | Text | Page title in Notion |
| Resolved | Checkbox | Mark as done after fixing |
| Error Type | Select | Type of issue: "File Upload Failed", "Invalid URL", "Table Split" |
| Block Link | URL | Direct link to the issue in Notion (page or block level) |

**Error Types**:

1. **File Upload Failed**
   - Files that couldn't be uploaded to Notion
   - Saved to `--unsupported-files` directory
   - Block Link points to the 🔧 user action marker in the page
   - Error Detail shows filename and local path

2. **Invalid URL**
   - URLs with unsupported schemes (e.g., `mongodb://localhost`, `file://`)
   - Marked inline with 🔗❌ icon in the page
   - Block Link points to page level (search for URL in Error Detail)
   - Evernote internal links (`evernote://`) are NOT treated as errors

3. **Table Split**
   - Tables exceeding Notion's limits (split into multiple tables)
   - May need manual review or consolidation

**Workflow**:

1. **Import notebooks** - Exception tracking is automatic
2. **Open "User Action Required" database** in Notion
3. **Work through items**:
   - Click Block Link to jump to the issue
   - For file uploads: manually upload from `NotionDocuments/` directory
   - For invalid URLs: fix or replace the link
   - Check Resolved when done
4. **Filter by Resolved** to see remaining items

**Per-Notebook Exception Pages**:

For informational errors (not requiring action), a child page is created under "Exceptions/" for each notebook listing:
- Note titles with partial imports
- Direct links to affected pages
- Inline error callouts in each page describe what went wrong

This system ensures **every note is imported** with clear tracking of any issues, making migration completion straightforward.

### Upload modes

The `--mode` option allows you to choose how to upload your notebooks: as databases or pages. `DB` mode is the default since Notion itself uses this mode when importing from Evernote. `PAGE` mode makes the tree feel like the original Evernote notebooks hierarchy.

Since `PAGE` mode does not benefit from having separate space for metadata, you can still preserve the note's original meta with the `--add-meta` option. It will attach a callout block with all meta info as a first block in each note [like this](https://imgur.com/a/lJTbprH).

### Web Clips

Due to Notion's limitations Evernote web clips cannot be uploaded as-is. `enex2notion` provides two modes with the `--mode-webclips` option:

- `TXT`, converting them to text, stripping all HTML formatting \[Default\]

  - similar to Evernote's "Simplify & Make Editable"

- `PDF`, converting them to PDF, keeping HTML formatting as close as possible

  - web clips are converted using [wkhtmltopdf](https://wkhtmltopdf.org/), see [this page](https://github.com/JazzCore/python-pdfkit/wiki/Installing-wkhtmltopdf) on how to install it

Since Notion's gallery view does not provide thumbnails for embedded PDFs, you have the `--add-pdf-preview` option to extract the first page of generated PDF as a preview for the web clip page.

### Banned file extensions

Notion prohibits uploading files with certain extensions. The list consists of extensions for executable binaries, supposedly to prevent spreading malware. `enex2notion` will automatically add a `bin` extension to those files to circumvent this limitation. List of banned extensions: `apk`, `app`, `com`, `ear`, `elf`, `exe`, `ipa`, `jar`, `js`, `xap`, `xbe`, `xex`, `xpi`.

### Misc

The `--condense-lines` option is helpful if you want to save up some space and make notes look more compact. [Example](https://imgur.com/a/sV0X8z7).

The `--condense-lines-sparse` does the same thing as `--condense-lines`, but leaves gaps between paragraphs. [Example](https://imgur.com/a/OBzeqn7).

The `--tag` option allows you to add a custom tag to all uploaded notes. It will add this tag to existing tags if the note already has any.

## Resolving Evernote Links

After importing your notes, you may have internal Evernote links (`evernote://` URLs) that are broken. The `resolve-links` command finds these links and replaces them with working Notion page links.

### What Links Are Resolved

The tool handles two formats of evernote:// links:

1. **Markdown format**: `[text](evernote://...)` - created by enex2notion
2. **Rich text links**: Text with `evernote://` URL - created by Notion's official importer

### Basic Usage

```shell
# Resolve all links (requires authentication)
$ enex2notion --resolve-links --use-env

# Or with token directly
$ enex2notion --resolve-links --token secret_your_token_here
```

### Command Options

```
--token TOKEN          Notion Integration token [REQUIRED]
--use-env              Use NOTION_TOKEN environment variable
--root-page NAME       Root page to scan (default: "Evernote ENEX Import")
--page NAME            Analyze only a specific page by name
--page-list FILE       Path to page list cache file (JSON)
--match-mode MODE      Matching strategy: exact, case-insensitive, fuzzy
                       (default: case-insensitive)
--dry-run              Show matches without updating links
--summary FILE         Save resolution report to file
--verbose              Show detailed match information
--log FILE             Save program log to file
```

### Page List Caching

For large imports, collecting all page names can take time. Use `--page-list` to cache the page map:

```shell
# First run: scan Notion and save page list
$ enex2notion --resolve-links --use-env --page-list pages.json

# Subsequent runs: load from cache (much faster)
$ enex2notion --resolve-links --use-env --page-list pages.json
```

### Analyzing a Single Page

To test or fix links in one page:

```shell
# Analyze a specific page
$ enex2notion --resolve-links --use-env --page "My Note Title" --dry-run

# Update links in that page
$ enex2notion --resolve-links --use-env --page "My Note Title"
```

### Matching Strategies

- **exact**: Case-sensitive exact match (most strict)
- **case-insensitive**: Ignores case differences (default, recommended)
- **fuzzy**: Uses similarity matching for close matches (most lenient)

```shell
# Use fuzzy matching for approximate matches
$ enex2notion --resolve-links --use-env --match-mode fuzzy
```

### Example Workflow

```shell
# 1. Import your notes
$ enex2notion --use-env my_notebooks/

# 2. Scan and cache all page names
$ enex2notion --resolve-links --use-env --page-list pages.json --dry-run

# 3. Review the report, then update links
$ enex2notion --resolve-links --use-env --page-list pages.json --summary report.txt

# 4. Fix unmatched links for a specific page
$ enex2notion --resolve-links --use-env --page "Problematic Note" --match-mode fuzzy
```

### Understanding the Report

The resolution report shows:
- Total pages scanned and pages with evernote:// links
- Matched links (successfully resolved)
- Unmatched links (link text doesn't match any page)
- For unmatched links: suggestions for manual resolution

## Examples

### Checking notes before upload

```shell
$ enex2notion --verbose my_notebooks/
```

### Uploading notes from a single notebook

```shell
$ enex2notion --token <YOUR_TOKEN_HERE> "notebook.enex"
```

### Uploading with the option to continue later

```shell
$ enex2notion --token <YOUR_TOKEN_HERE> --done-file done.txt "notebook.enex"
```

## Getting help

If you found a bug or have a feature request, please [open a new issue](https://github.com/vzhd1701/enex2notion/issues/new/choose).

If you have a question about the program or have difficulty using it, you are welcome to [the discussions page](https://github.com/vzhd1701/enex2notion/discussions). You can also mail me directly, I'm always happy to help.
