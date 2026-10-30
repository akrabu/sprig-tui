# Sprig

Sprig is a Workflowy-inspired terminal outliner with unlimited nesting and zoom,
fuzzy search, labels, stars, and native OPML files.
The Python distribution and GitHub repository are named **sprig-tui**.

## Install and run

Requires Python 3.10 or newer, curses, and a UTF-8 terminal on Linux or macOS.
Brotli is installed as a dependency for compressed backups.

```bash
git clone https://github.com/akrabu/sprig-tui.git
cd sprig-tui
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/sprig my-notes.opml
```

You can also use `python -m sprig` or `./sprig.py` with the installed environment.
Without a filename, Sprig opens or creates `outline.opml` in the current folder.
Try `sprig examples/demo.opml` for a fictional sample.

Type directly into a bullet; there is no insert mode. **Esc opens the File /
Node / Navigate menu bar.** Use Left/Right to choose a menu, Up/Down to choose
an action, Enter to run it, and Esc to close it. Menus and dialogs have double
borders, with single separators between action groups. Mouse selection works
while the menu is open. F1 opens keyboard help; Ctrl+Q quits from any screen.

## Saving, autosave, and backups

The original OPML changes on **manual save or save-on-exit**, including a file
switch or close. Timed autosave creates snapshots of the unsaved buffer.
An untouched new document is not written merely by opening or quitting it.

| Mechanism | Default location | Behavior |
| --- | --- | --- |
| Autosave snapshots | `~/.config/sprig-tui/autosave/` | Uncompressed OPML, every 60 seconds while edits are pending; keeps 20 versions per source file. |
| Full backups | `~/.config/sprig-tui/backup/` | Brotli snapshots on manual save and saving on exit/switch; keeps 20 versions per source file. |
| Crash checkpoints | `~/.config/sprig-tui/recovery/` | Uncompressed checksummed recovery records after idle edits, targeting two-second intervals during continuous editing. |

Choose **File → Autosave and backup settings** to change either directory,
the autosave interval, retention counts, whether snapshots/full backups are
enabled, and whether to save automatically on exit or file switch. Changes are
remembered centrally. Absolute paths use the chosen location; relative paths
are resolved beside the source OPML. CLI options override preferences for one
run; changing a preference in the menu takes effect immediately.

```bash
sprig --autosave-interval 120 notes.opml
sprig --autosave-dir ~/snapshots --backup-dir ~/archives notes.opml
sprig --backup-count 50 --autosave-count 10 notes.opml
sprig --no-save-on-exit notes.opml
sprig --no-autosave notes.opml
```

Full backups use `<original stem>_YYYY-MM-DD_HHMMSS.opml.br`. Autosave snapshots
use the same naming pattern ending in `.opml`. Both live in per-source
subdirectories containing a path hash, so two `notes.opml` files keep separate
histories. Identical consecutive snapshots are reused. Same-second saves use
successive available timestamps. Each snapshot has a `.sha256` sidecar and is
verified before old versions are rotated. Full backups retain the previous
bytes before replacement and the newly saved document; a `.opml.bak` beside the
source also retains the latest previous bytes.

**File → Backups and autosaves** lists snapshots for the open document. Select
one and confirm to restore it into an unsaved buffer; Ctrl+S commits it.
**File → Recover unsaved document** lists inactive crash checkpoints. At startup,
Sprig offers recovery for the current file. Recovery preserves notes, metadata,
and private view state. Another running session's checkpoint is never offered.
Recovered changes stay unsaved until you save. If the original changed since a
crash checkpoint, saving reports a conflict; F4 saves a separate copy.

With Save on exit disabled, opening, closing, and quitting offer Save, Discard,
or Esc to cancel. A failed save keeps edits open. Atomic replacement, file and
directory sync, cooperative write locks, and external-change checks protect the
source. A failure after replacement is reported as a saved-file warning, and a
conflict preserves the external file. Crash recovery still operates when timed
autosave snapshots are disabled. A sudden power loss can lose edits since the
last completed checkpoint.

## Keys and navigation

| Action | Shortcut | Alternative |
| --- | --- | --- |
| Menu bar | Esc | Click the bar while open |
| New / split bullet | Enter | |
| Indent / outdent branch | Tab / Shift+Tab | Alt+Shift+Right / Left |
| Zoom in / out | Alt+. / Alt+, | F7 / F8; click bullet / breadcrumbs |
| Back / forward | Alt+Left / Alt+Right | Alt+B / Alt+F; Navigate menu |
| Return Home | F12 | Click Home |
| Collapse / expand current | Ctrl+Up / Ctrl+Down | Click triangle |
| Toggle collapse | Ctrl+Space | F9 |
| Collapse / expand all below | Ctrl+Alt+Up / Down | Node menu |
| Move branch among siblings | Alt+Shift+Up / Down | Alt+Up / Down |
| Move to / copy to | Ctrl+Alt+M / C | Node menu destination pickers |
| Edit note / return to bullet | Shift+Enter | F6 |
| Complete / uncomplete | Ctrl+Enter | F5 |
| Show / hide completed | Ctrl+O | Navigate menu |
| Duplicate subtree | Ctrl+D | Alt+Shift+D |
| Delete subtree | F10 | Ctrl+Shift+Backspace |
| Star / unstar | Alt+S | Node menu |
| List starred nodes | F11 | Navigate menu |
| Labels picker | Ctrl+T | Navigate menu |
| Fuzzy search | Ctrl+F / Ctrl+K | Navigate menu |
| Undo / redo | Ctrl+Z / Ctrl+Y | |
| Save / save as | Ctrl+S / F4 | F2 saves |
| Open / close file | F3 / Ctrl+W | File menu |
| Help / quit | F1 / Ctrl+Q | Menus |

Function keys work when a terminal cannot distinguish modified Enter keys.
On macOS, configure the terminal to send Option as Alt/Meta for Alt shortcuts.

Enter splits a bullet at the cursor. Tab moves it and all descendants beneath
its preceding sibling; Shift+Tab moves the branch out one level. Enter on an
empty nested bullet also outdents it. Text wraps to the terminal width, and deep
indentation shifts to keep the selected text visible.

Zooming makes a bullet the editable page title. Enter on that title creates a
child. Saving always includes the entire document. Back/forward remembers file,
zoom, selected node, cursor, and filters, while retaining current document edits
and undo history. Crossing to another file uses the usual save/discard flow.
History is limited to 200 locations per direction for the current session.

Move to and Copy to offer fuzzy destinations within the current document. The
branch is appended as a child of the chosen destination; Home appends a top-level
bullet. A move cannot target the branch itself or its descendants. Each transfer
is one undoable change. Copy to and Duplicate give every copied node a new private
identity while preserving its OPML attributes and descendants.

## Notes and editing

Notes edit inline. Enter inside a note adds a newline; F6 returns to the bullet.
Shift+Left/Right and Shift+Home/End select text; Ctrl+A selects the whole field.
Ctrl+C/X/V use an internal clipboard. Without text selected they copy, cut, or
paste whole subtrees. Cutting a subtree requires deletion confirmation and a
trash backup. The terminal's system-paste shortcut pastes external text;
indented bracketed pastes become nested bullets. Note pastes retain newlines.

Ctrl+B, Alt+I, and Ctrl+U format selected text as bold, italic, and underline,
or toggle formatting on the whole field. Backspace at the start merges with the
preceding sibling when metadata can be retained. Undo keeps up to 200 editing
groups, sharing unchanged records to reduce memory use on large documents.

## Search, hashtags, mentions, and stars

Ctrl+F opens ranked fuzzy search over titles and notes, including collapsed and
completed branches. Type a subsequence such as `mrr` to find `mirror`.
Results show file and ancestor context, highlight title matches, and preview
matching notes. Enter reveals a result. Tab / Shift+Tab cycles search scope:

| Scope | Searches |
| --- | --- |
| Current branch | The zoomed node and all descendants; at Home this equals Whole file. |
| Whole file | Every node and note in the open document; initial default. |
| Global | OPML files beside the current document, including its unsaved buffer. Subfolders, trash, and backup files are excluded. |

The last scope is remembered. Opening another file's result uses the save/discard
flow. Unreadable files are reported and skipped. Import-preview search stays in
the import source and does not change the remembered scope.

`#hashtags` are cyan; `@mentions` are magenta. Both are searchable labels,
including Unicode and hyphenated or slash-separated names. Email addresses and
URL fragments are not labels. Typing `#` or `@` offers existing labels; arrows
choose, Tab/Enter accepts, and Esc dismisses the suggestions.

Click a label or choose one with Ctrl+T to filter. Multiple labels combine with
AND. Ancestors remain visible and matching folded branches are revealed without
changing their saved fold state. A bullet whose label you edit remains visible
until you navigate away. Navigate → Clear label filters restores the normal view.
Explicit label queries such as `#work @sky` match complete labels.

Alt+S stars the current node. F11 lists stars across remembered documents; Enter
opens and zooms to one. Ctrl+D in this list removes a bookmark, including one
whose file is unavailable. Stars are private metadata and do not alter OPML.

## Import and export

File → Import OPML opens a read-only preview. Browse with arrows, Tab to fold,
F7/F8 to zoom, and Ctrl+F to search. F12 returns to the Entire file heading.
Select Entire file for all top-level trees, or select a node for just that
subtree, then confirm the bullet count.

Import inserts immediately after the selection as a sibling. A leaf without
text, notes, children, additional attributes, or XML metadata is replaced.
Blank parents and bullets containing notes are retained. Importing after a
zoomed title zooms out to show the new sibling. Import is one undoable change
and preserves attributes, formatting, notes, completion, and child order.

File → Export TXT / Export Markdown offers the selected branch, current zoom,
or whole document. Existing destinations require confirmation. Exports follow
Workflowy's title and nested-bullet layout, with two-space indentation, quoted
TXT notes, and blockquoted Markdown notes. Markdown supports emphasis, links,
and completed tasks. Dates retain the source OPML text; no missing time-of-day
is invented. Invisible date-rendering markers are removed. Fictional matching
OPML, TXT, and Markdown examples are in `examples/`.

## Trash and restore

F10 confirms the branch name and total bullet count. Before deletion, Sprig
appends the full tree to `trash.opml` beside the original, recording source path,
parent, position, deletion time, and starred descendants. A backup failure
cancels deletion. Undo can restore it during the current session.

Navigate → Trash / Restore offers original-location or current-position restore.
An unavailable or ambiguous original parent falls back to the document root.
Cross-file restoration uses the normal file-switch flow. Records remain in the
trash after restore, so undo or discarded edits cannot erase the backup. The
trash is ordinary OPML with wrapper metadata around each backed-up tree.

## Compatibility and configuration

Workflowy's `text`, `_note`, `_complete`, unknown attributes, head metadata,
comments, processing instructions, and extension elements are retained. XML
whitespace and namespace prefixes may be reformatted. OPML must be valid XML,
with one body and a text attribute on every outline. DTDs and mixed XML text in
outline containers are rejected. Documents and decompressed backups are limited
to 64 MiB. Sprig has no fixed outline-depth limit.

Private state uses `~/.config/sprig-tui/`: `settings.json` stores preferences and
`files/<path-hash>.json` stores stars, folding, zoom, cursor, filters, and completed
visibility. `XDG_CONFIG_HOME` and `SPRIG_CONFIG_DIR` are respected. Existing
`outliner-tui` settings and stars are read as a migration fallback; saving writes
the new Sprig directory. The legacy files are retained. `OUTLINER_CONFIG_DIR`
remains an environment-variable alias.

Metadata uses private file permissions and atomic writes. Corrupt or newer
formats are preserved and reported. Conflicting writes from another session are
rejected. Stars follow content and ancestor context after external edits;
ambiguous stars are retained as unavailable bookmarks rather than guessed.

Sprig edits local exports; it does not synchronize with a Workflowy account.
Mirrors, boards, attachments, collaboration, and multi-node selection are not
implemented. `#template` is an ordinary label; templates are deferred. Future
ideas are in `todo.txt`.

## Development

```bash
.venv/bin/python -B -m unittest discover -v
sprig --check examples/demo.opml
```

Tests use fictional fixtures and temporary files, including an 8,852-node
outline with 915 notes, 32 completed items, 15 levels, and long rich-text fields.
Coverage includes OPML round trips, imports/exports, stars, search, labels,
recovery, locking, concurrent edits, write failures, retention, navigation, and
real curses sessions in pseudo-terminals. Supplied private exports are excluded
from the repository. See `REVIEW.md` for the data-safety review.

Licensed under MIT.
