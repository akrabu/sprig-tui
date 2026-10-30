# Sprig 0.3.0 data-safety review

Reviewed the tree model, OPML serializer, save/export paths, metadata migration,
trash/restore, import, snapshots, crash recovery, history, and curses commands.
The public test suite uses fictional data. Supplied private OPML and export
fixtures were checked locally and are excluded from publication.

## Findings fixed

- Tree edits now roll back if an operation fails partway through. Parent changes
  detach nodes safely, cycles are rejected, and merging moves every child.
  Validation rejects cycles, duplicate identities, and inconsistent parent links
  before serialization. Empty edits and pastes preserve metadata-bearing bullets.
- OPML encoding is iterative, retains unknown attributes and XML extensions in
  their relative positions, and validates the final XML before replacement.
  Foreign namespaced elements named `body` or `outline` remain extensions.
  Namespaced OPML remains namespaced. DTDs, malformed XML, mixed container text,
  missing outline text, and oversized documents fail without replacing a source.
- Saves and exports use same-directory temporary files, file sync, atomic rename,
  and directory sync. Cooperative locks serialize Sprig writers; hashes detect
  external edits both before writing and immediately before replacement.
  Overwrite confirmations retain the destination hash and reject later changes.
  A successful rename followed by a directory-sync error updates the in-memory
  file identity and saved state while reporting the durability warning.
- Manual and exit saves preserve old bytes before replacement and create a
  Brotli copy of the saved document. Full backups and uncompressed autosave
  snapshots use separate configurable directories and retention counts.
  Per-source path hashes isolate identically named files. Collision handling
  and monotonically advancing timestamps prevent overwriting or misordering
  snapshots during rapid saves or clock changes. Checksum verification precedes
  retention cleanup; malformed/incomplete snapshots are preserved.
- Autosave snapshots never modify the source OPML. Frequent crash checkpoints
  retain the unsaved buffer and current private view state. Session locks hide
  live checkpoints. Recovery validates checksums and OPML before changing a
  buffer, retains the original disk fingerprint, and cannot silently overwrite
  newer external changes. Failed saves retain edits and recovery data.
- JSON schema/version checks preserve unreadable or unsupported state files.
  Metadata writes detect concurrent updates, including after browsing Stars.
  Migration reads legacy state and writes Sprig's new directory while retaining
  old files. Ambiguous stars and parent references are preserved or fall back
  safely instead of guessing. Discarding a buffer does not persist its view over
  the original document's metadata.
- Deletion and subtree cuts require confirmation and a successful trash write
  before editing the tree. Restore copies the archived branch without removing
  the record. Imports replace only genuinely empty leaves and are one undo step.
- Move destinations exclude the moving subtree; transfers validate the target
  and preserve notes, metadata, and descendants. Back/forward stores locations,
  never document contents. Cancelled file changes retain the buffer and history.
- Search narrows incremental ordinary queries without dropping valid label
  matches. Unicode case-fold expansions map highlights back to original character
  offsets. Invalid terminal codepoints are ignored. The layout keeps a window
  around the selection to avoid formatting an entire large account at once.

## Verification

- The public publication snapshot passed 102 tests on CPython 3.11.2 / Linux
  aarch64, with no access to the private exports or working OPML files.
- Static checks for undefined names and unused bindings passed with Ruff.
- Hosted checks also cover Python 3.10 and 3.13 on Linux and Python 3.13 on
  macOS. Test paths use canonical names so macOS's `/var` alias does not disable
  failure injection. Process-exit waits recheck predicates to handle fast exits
  between polling calls. Relative configuration roots are resolved consistently.
- Source distributions include the complete test helpers and fictional examples,
  with private documents excluded.
- Unit and integration checks cover tree edits and rollback, deep outlines,
  OPML preservation, XML errors, save conflicts, failed replacement, simulated
  post-rename sync failure, locks, export confirmation races, metadata conflicts,
  legacy migration, checksum failures, retention, recovery, and navigation.
- Real pseudo-terminal tests exercise normal editing, Unicode, resizing, deep
  zoom, imports/exports, trash/restore, stars, autosave, save-on-exit, failed saves,
  and killing a process followed by in-app recovery.
- A fictional large fixture contains 8,852 nodes, 915 notes, 32 completed items,
  15 levels, and long rich-text fields. A separate local check against the
  supplied full account retained all attributes, child counts, and head metadata;
  a real-terminal edit/undo/save retained every outline record.
- The supplied TXT fixture matches byte for byte. The supplied Markdown fixture
  matches hierarchy and notes; its date formatting invents a noon time absent
  from OPML, so Sprig retains the actual source date text.
- All supplied original files remained byte-for-byte unchanged during checks.

## Practical limits

Crash recovery can preserve only the last completed checkpoint. Filesystem and
storage hardware determine power-loss durability. Sprig locks coordinate Sprig
sessions; another application that ignores those locks can still write in the
small interval between the final hash check and rename. Keep one writer per file
when using other editors or synchronization programs. Tests inject common write
failures but do not emulate every filesystem or physical power failure.

Validation and testing do not prove that all possible corruption is impossible.
There is no live Workflowy synchronization or automated account-side import test.
XML whitespace and namespace prefixes may be reformatted. Mirror semantics and
Workflowy's non-outline views are outside this release.
