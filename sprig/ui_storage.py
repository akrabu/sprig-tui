"""Recovery, snapshot preferences, and safe navigation between documents."""

from pathlib import Path
import math
import time

from .backups import Backups
from .history import History, Location
from .opml import OpmlFile
from .recovery import Recovery
from .storage import digest, read_limited


class StorageActions:
    def init_storage(self, options: dict) -> None:
        settings = self.store.settings
        self.autosave = options.get("autosave")
        if self.autosave is None:
            self.autosave = settings.get("autosave_enabled", True)
        self.autosave_delay = options.get("autosave_interval") or settings.get("autosave_interval", 60.0)
        self.save_on_exit = options.get("save_on_exit")
        if self.save_on_exit is None:
            self.save_on_exit = settings.get("save_on_exit", True)
        self.storage_overrides = {key: value for key, value in options.items()
                                  if value is not None and key not in ("autosave", "autosave_interval", "save_on_exit")}
        self.history = History()
        self.recovery = Recovery(self.store.directory)
        self.recovered = False
        self.exit_discarded = self._discarding = False
        self.last_checkpoint = self.last_autosave = time.monotonic()
        self._autosave_key = None
        self.configure_storage()

    def preference(self, key, default):
        return self.storage_overrides.get(key, self.store.settings.get(key, default))

    def configure_storage(self) -> None:
        self.source.backup_directory = self.preference("backup_directory", self.store.directory / "backup")
        self.source.backup_count = self.preference("backup_count", 20)
        self.source.backups_enabled = self.preference("backups_enabled", True)

    def autosaves(self):
        return Backups(self.source.path, self.preference("autosave_count", 20),
                       self.preference("autosave_directory", self.store.directory / "autosave"), compressed=False)

    def checkpoint(self) -> None:
        if self.closed or not self.editor.dirty or self.exit_discarded:
            return
        try:
            self.recovery.write(self.source, self.editor, self.store)
        except (OSError, ValueError) as error:
            self.status(f"Crash recovery checkpoint failed: {error}", True)
        self.last_checkpoint = time.monotonic()

    def clear_recovery(self) -> None:
        try:
            self.recovery.clear()
        except OSError as error:
            self.status(f"Could not clear recovery snapshot: {error}", True)

    def storage_tick(self) -> bool:
        if self.closed or not self.editor.dirty:
            return False
        now = time.monotonic()
        # Frequent checkpoints are separate from the user-configured OPML snapshots.
        if now - self.last_checkpoint >= 2 or (now - self.last_edit >= 0.35 and now - self.last_checkpoint >= 0.35):
            self.checkpoint()
        key = (id(self.editor), self.editor.revision)
        if not self.autosave or now - self.last_autosave < self.autosave_delay or key == self._autosave_key:
            return False
        self.last_autosave = now
        try:
            snapshots = self.autosaves()
            snapshots.create(self.source.encode(self.editor))
            snapshots.prune()
        except (OSError, ValueError) as error:
            self.status(f"Autosave snapshot failed: {error}", True)
            return True
        self._autosave_key = key
        self.status("Autosave snapshot created")
        return True

    def offer_recovery(self) -> None:
        records, errors = self.recovery.pending(self.source.path)
        if errors:
            self.status("Some recovery snapshots are unreadable and were preserved: " + errors[0], True)
        if records:
            self.ask("Unsaved recovery snapshot available", "[y] browse recovery · [n] continue",
                     lambda answer: self.recovery_browser() if answer == "y" else None, choices=True)

    def recovery_browser(self) -> None:
        from .ui_actions import Item
        records, errors = self.recovery.pending()
        if errors:
            self.status("Unreadable snapshots preserved: " + errors[0], True)
        items = [Item(Path(data["file"]).name, path, data["saved_at"] + " · " + data["file"])
                 for path, data in records]
        def choose(item):
            self.ask("Recover unsaved document?", "[y] restore buffer · [n] cancel",
                     lambda answer: self.restore_recovery(item.value) if answer == "y" else None, choices=True)
        self.pick("Crash recovery", items, choose)

    def restore_recovery(self, path: Path) -> None:
        # Validate fully before asking to discard anything in the current buffer.
        try:
            data = self.recovery.read(path)
            OpmlFile.decode(data["file"], data["opml"].encode())
        except (OSError, ValueError) as error:
            self.status(f"Recovery failed; snapshot preserved: {error}", True)
            return
        def restore():
            try:
                source, editor, view = self.recovery.load(path)
            except (OSError, ValueError) as error:
                self.status(f"Recovery failed; snapshot preserved: {error}", True)
                return
            self.persist_view()
            # Do not clear the newly claimed recovery record during this switch.
            self.document_loaded(source, editor, view=view, preserve_recovery=True)
            self.editor.mark_unsaved()
            self.recovered = True
            self.last_edit = time.monotonic()
            self.checkpoint()
            self.status("Recovered unsaved buffer. Ctrl+S saves; F4 keeps a separate copy.")
        self.guard_changes(restore)

    def snapshot_browser(self) -> None:
        from .ui_actions import Item
        backups = Backups(self.source.path, self.source.backup_count, self.source.backup_directory)
        items = [Item(path.name, path, label) for label, manager in
                 (("Full backup · Brotli", backups), ("Autosave · OPML", self.autosaves()))
                 for path in reversed(manager.paths())]
        def choose(item):
            self.ask("Restore document snapshot?", "[y] restore buffer · [n] cancel",
                     lambda answer: self.restore_snapshot(item.value) if answer == "y" else None, choices=True)
        self.pick("Backups and autosaves · current file", items, choose)

    def restore_snapshot(self, path: Path) -> None:
        try:
            content = Backups.read(path)
            source, editor = OpmlFile.decode(self.source.path, content)
        except (OSError, ValueError) as error:
            self.status(f"Cannot restore snapshot: {error}", True)
            return
        def restore():
            try:
                source.fingerprint = digest(read_limited(source.path))
            except FileNotFoundError:
                source.fingerprint = None
            except (OSError, ValueError) as error:
                self.status(f"Cannot read original document: {error}", True)
                return
            self.persist_view()
            self.document_loaded(source, editor)
            editor.mark_unsaved()
            self.recovered = True
            self.last_edit = time.monotonic()
            self.checkpoint()
            self.status("Snapshot restored into buffer; Ctrl+S saves it.")
        self.guard_changes(restore)

    def backup_settings(self) -> None:
        from .ui_actions import Item
        choices = [
            Item("Autosave snapshots: " + ("on" if self.autosave else "off"), "autosave_enabled"),
            Item(f"Autosave interval: {self.autosave_delay:g} seconds", "autosave_interval"),
            Item("Autosave directory…", "autosave_directory", str(self.preference("autosave_directory", self.store.directory / "autosave"))),
            Item(f"Autosave snapshots kept: {self.preference('autosave_count', 20)}", "autosave_count"),
            Item("Full backups: " + ("on" if self.source.backups_enabled else "off"), "backups_enabled"),
            Item("Backup directory…", "backup_directory", str(self.source.backup_directory)),
            Item(f"Full backups kept: {self.source.backup_count}", "backup_count"),
            Item("Save on exit / file switch: " + ("on" if self.save_on_exit else "off"), "save_on_exit"),
        ]
        def choose(item):
            key = item.value
            if key in ("autosave_enabled", "backups_enabled", "save_on_exit"):
                value = {"autosave_enabled": self.autosave, "backups_enabled": self.source.backups_enabled,
                         "save_on_exit": self.save_on_exit}[key]
                self.set_storage_preference(key, not value)
                self.backup_settings()
            else:
                default = {"autosave_interval": self.autosave_delay, "autosave_count": self.preference("autosave_count", 20),
                           "backup_count": self.source.backup_count}.get(key, item.detail)
                self.ask(item.title.split(":")[0], str(default), lambda text: self.set_storage_preference(key, text))
        self.pick("Autosave and backup settings", choices, choose)

    def set_storage_preference(self, key: str, value) -> None:
        try:
            if key == "autosave_interval":
                value = float(value)
                if not math.isfinite(value) or value < 0.1:
                    raise ValueError("Interval must be at least 0.1 seconds")
            elif key in ("backup_count", "autosave_count"):
                value = int(value)
                if not 1 <= value <= 10000:
                    raise ValueError("Keep between 1 and 10000 snapshots")
            elif key.endswith("_directory"):
                if not value.strip():
                    value = str(self.store.directory / ("backup" if key.startswith("backup") else "autosave"))
                Path(value).expanduser()  # Validate the path before remembering it.
                if "\x00" in value:
                    raise ValueError("Invalid directory")
        except (ValueError, TypeError) as error:
            self.status(str(error), True)
            return
        self.store.settings[key] = value
        self.storage_overrides.pop(key, None)
        if key == "autosave_enabled":
            self.autosave = value
        elif key == "autosave_interval":
            self.autosave_delay = value
        elif key == "save_on_exit":
            self.save_on_exit = value
        self._autosave_key = None
        self.configure_storage()
        if self.settings_changed():
            self.status("Preference updated")

    def navigate_history(self, forward=False) -> None:
        self._navigated_history = True
        stack = self.history.forward if forward else self.history.back
        other = self.history.back if forward else self.history.forward
        if not stack:
            self.status("No forward history" if forward else "No back history")
            return
        target = stack[-1]
        current = Location.capture(self)
        def arrive():
            target.apply(self.editor)
            stack.pop()
            if current:
                other.append(current)
                del other[:-200]
            self.scroll = 0
            self.clear_selection()
        if not self.closed and target.path == self.source.path:
            arrive()
        elif not target.path.is_file():
            self.status("History file is unavailable; history preserved", True)
        else:
            self.switch_file(str(target.path), arrive, record_history=False)

    def transfer_picker(self, *, copy=False) -> None:
        from .ui_actions import Item
        from .search import Index
        e = self.editor
        selected = e.selected
        if selected is e.root:
            return
        excluded = set() if copy else {n.uid for n in selected.walk()}
        index = Index(e.root, self.source.path)
        items = [Item("Home (document root)", e.root, "Append as a top-level bullet")]
        items += [Item(title or "(empty bullet)", node, parents)
                  for node, title, _, parents in index.entries if node.uid not in excluded]
        def choose(item):
            if self.editor.selected is not selected:
                self.status("Selection changed; choose the branch again", True)
                return
            try:
                self.change(lambda: e.transfer(item.value, copy=copy))
            except ValueError as error:
                self.status(str(error), True)
                return
            self.scroll = 0
            self.clear_selection()
            self.status("Copied branch" if copy else "Moved branch")
        self.pick("Copy branch into…" if copy else "Move branch into…", items, choose)
