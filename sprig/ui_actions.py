"""Menus, file workflows and reusable searchable pickers for the curses editor."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
import curses
import json
import re
import time
from typing import Callable

from .exports import render, save_export
from .keys import COLLAPSE_KEYS, EXPAND_KEYS, Event
from .layout import width
from .model import Editor, Node
from .opml import OpmlFile
from .search import Hit, Index, fuzzy, label_spans
from .state import StateStore, materialize, references, resolve
from .trash import Trash
from .history import Location
from .ui_storage import StorageActions
from .storage import digest, read_limited


@dataclass
class Item:
    title: str
    value: object = None
    detail: str = ""


@dataclass
class Picker:
    title: str
    items: list[Item]
    choose: Callable[[Item], None]
    kind: str = "list"
    query: str = ""
    cursor: int = 0
    selected: int = 0
    matches: list[Item] = field(default_factory=list)
    help: str = "↑↓ choose · Enter select · Esc cancel"
    directory: Path | None = None


@dataclass
class Preview:
    source: OpmlFile
    editor: Editor
    entire: Node
    kind: str
    choose: Callable[[Node], None]
    scroll: int = 0
    rows: list = field(default_factory=list)


class Actions(StorageActions):
    def init_actions(self) -> None:
        self.store = StateStore()
        self.store.bind(self.source, self.editor)
        self.closed = False
        self.menu_open = False
        self.menu_group = self.menu_index = 0
        self.menu_hits = []
        self.picker: Picker | None = None
        self.preview: Preview | None = None
        self.completion: list[str] = []
        self.completion_index = 0
        self.completion_range = (0, 0)
        self.label_hits = []
        self._label_cache = None
        self._index_cache = None
        self._global_cache = {}
        self.scope = self.store.settings.get("search_scope", "Whole file")
        if self.scope not in ("Current branch", "Whole file", "Global"):
            self.scope = "Whole file"
        if self.store.blocked:
            self.status("Configuration issue: " + next(iter(self.store.blocked.values())), True)
        elif self.store.unresolved:
            self.status("Some stars could not be matched after external edits; their records are preserved.", True)

    def ask(self, title: str, text: str, callback, *, choices: bool = False, cancel=None) -> None:
        from .ui import Prompt
        self.prompt = Prompt(title, text, len(text), callback, choices, cancel)

    def persist_view(self) -> None:
        if self.closed or self._discarding or (self.recovered and self.editor.dirty):
            return
        try:
            self.store.save_view(self.source, self.editor)
        except (OSError, ValueError) as error:
            self.status(f"Could not save view state: {error}", True)

    def settings_changed(self) -> bool:
        try:
            self.store.save_settings()
        except (OSError, ValueError) as error:
            self.status(f"Could not save preferences: {error}", True)
            return False
        return True

    def document_loaded(self, source: OpmlFile, editor: Editor, *, view=None, preserve_recovery=False) -> None:
        if not preserve_recovery:
            self.clear_recovery()
        self.source, self.editor = source, editor
        self.store.bind(source, editor, data=view)
        self.configure_storage()
        self.recovered = False
        self.closed = self.autosave_paused = False
        self.scroll = 0
        self.menu_open = False
        self.completion.clear()
        self.picker = self.preview = None
        self._label_cache = self._index_cache = None
        self.clear_selection()
        self.store.settings["last_directory"] = str(source.path.parent)
        if self.settings_changed():
            self.status(f"Opened {source.path.name}")
        self.last_autosave = self.last_checkpoint = time.monotonic()
        self._autosave_key = None

    def switch_file(self, path: str, then=None, *, record_history=True) -> None:
        if not path.strip():
            return
        try:
            OpmlFile.open(path)  # Validate before offering to discard any edits.
        except (OSError, ValueError) as error:
            self.status(str(error), True)
            return

        before = Location.capture(self)
        def switch() -> None:
            self.persist_view()
            try:
                source, editor = OpmlFile.open(path)
            except (OSError, ValueError) as error:
                self.status(str(error), True)
                return
            self.document_loaded(source, editor)
            if then:
                then()
            if record_history:
                self.history.record(before, Location.capture(self))

        self.guard_changes(switch)

    def close_file(self) -> None:
        def close() -> None:
            self.persist_view()
            self.clear_recovery()
            self.closed = True
            self.editor = Editor()
            self.scroll = 0
            self.clear_selection()
            self.menu_group = self.menu_index = 0
            self.menu_open = True
        self.guard_changes(close)

    def new_file(self) -> None:
        def create(path: str) -> None:
            if not path.strip():
                return
            target = Path(path).expanduser()
            if not target.suffix:
                target = target.with_suffix(".opml")
            if target.exists():
                self.status("That file exists; use File → Open.", True)
                return
            self.switch_file(str(target))
        text = str(self.source.path.parent / "untitled.opml")
        self.ask("New OPML filename", text, create)

    def menus(self):
        key = self.keymap.label
        return [
            ("File", [
                ("New…", "", self.new_file, True),
                ("Open…", key("open"), lambda: self.file_browser(), True),
                ("Close", key("close"), self.close_file, not self.closed),
                ("Save", key("save"), self.save, not self.closed),
                ("Save as…", key("save_as"), lambda: self.ask("Save as", str(self.source.path), self.save_as), not self.closed),
                ("Import OPML…", "", lambda: self.file_browser(importing=True), not self.closed),
                ("Export TXT…", "", lambda: self.export_dialog("txt"), not self.closed),
                ("Export Markdown…", "", lambda: self.export_dialog("md"), not self.closed),
                ("Autosave and backup settings…", "", self.backup_settings, True),
                ("Recover unsaved document…", "", self.recovery_browser, True),
                ("Backups and autosaves…", "", self.snapshot_browser, not self.closed),
                ("Reload config…", "", self.reload_config, True),
                ("Quit", key("quit"), lambda: self._handle_event(Event("ctrl+q")), True),
            ]),
            ("Node", [
                ("Star / unstar", key("star"), self.toggle_star, not self.closed),
                ("Duplicate subtree", key("duplicate"), lambda: self.change(self.editor.duplicate), not self.closed),
                ("Delete subtree…", key("delete"), self.request_delete, not self.closed),
                ("Expand current", key("expand"), lambda: self.fold(True), not self.closed),
                ("Collapse current", key("collapse"), lambda: self.fold(False), not self.closed),
                ("Expand all below", key("expand_all"), lambda: self.fold(True, True), not self.closed),
                ("Collapse all below", key("collapse_all"), lambda: self.fold(False, True), not self.closed),
                ("Move to…", key("move_to"), self.transfer_picker, not self.closed),
                ("Copy to…", key("copy_to"), lambda: self.transfer_picker(copy=True), not self.closed),
            ]),
            ("Navigate", [
                ("Search…", key("search"), self.open_search, not self.closed),
                ("Starred nodes…", key("stars"), self.star_browser, True),
                ("Hashtags and mentions…", key("labels"), self.label_browser, not self.closed),
                ("Clear label filters", "", self.clear_filters, not self.closed),
                ("Trash / Restore…", "", self.trash_browser, True),
                ("Show / hide completed", key("show_completed"), self.toggle_completed, not self.closed),
                ("Home", key("home"), self.editor.home, not self.closed),
                ("Keyboard help", key("help"), lambda: setattr(self, "help_open", True), True),
                ("Back", key("back"), self.navigate_history, bool(self.history.back)),
                ("Forward", key("forward"), lambda: self.navigate_history(True), bool(self.history.forward)),
            ]),
        ]

    def menu_event(self, event: Event) -> None:
        menus = self.menus()
        if event.key == "escape":
            self.menu_open = False
        elif event.key in ("left", "right", "tab", "shift+tab"):
            step = -1 if event.key in ("left", "shift+tab") else 1
            self.menu_group = (self.menu_group + step) % len(menus)
            self.menu_index = 0
        elif event.key in ("up", "down", "home", "end"):
            size = len(menus[self.menu_group][1])
            self.menu_index = (self.menu_index + (-1 if event.key == "up" else 1)) % size
            if event.key in ("home", "end"):
                self.menu_index = 0 if event.key == "home" else size - 1
        elif event.key == "enter":
            _, _, action, enabled = menus[self.menu_group][1][self.menu_index]
            if enabled:
                self.menu_open = False
                action()
        elif event.key == "text":
            value = event.text.casefold()
            items = menus[self.menu_group][1]
            for offset in range(1, len(items) + 1):
                index = (self.menu_index + offset) % len(items)
                if items[index][0].casefold().startswith(value):
                    self.menu_index = index
                    break
        elif event.key == "mouse":
            try:
                _, x, y, _, state = curses.getmouse()
            except curses.error:
                return
            if state & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
                for yy, left, right, group, index in self.menu_hits:
                    if yy == y and left <= x < right:
                        self.menu_group = group
                        if index is None:
                            self.menu_index = 0
                        else:
                            self.menu_index = index
                            self.menu_event(Event("enter"))
                        break

    def draw_menu(self, columns: int) -> None:
        self.put(0, 0, " " * columns)
        menus = self.menus()
        self.menu_hits = []
        x = 2
        for index, (name, _) in enumerate(menus):
            self.put(0, x, f" {name} ", curses.A_REVERSE if index == self.menu_group else self.color(1))
            self.menu_hits.append((0, x, x + len(name) + 2, index, None))
            x += len(name) + 4
        box_width = min(58, columns - 4)
        group_x = min((2, 10, 18)[self.menu_group], columns - box_width - 2)
        items = menus[self.menu_group][1]
        boundaries = ((3, 5, 8, 12), (1, 3, 7), (4, 6, 8))[self.menu_group]
        visual = []
        for index in range(len(items)):
            if index in boundaries:
                visual.append(None)
            visual.append(index)
        capacity = max(1, self.screen.getmaxyx()[0] - 4)
        first = max(0, visual.index(self.menu_index) - capacity + 1)
        shown = visual[first:first + capacity]
        self.box(1, group_x, len(shown) + 2, box_width)
        for offset, index in enumerate(shown):
            y = offset + 2
            if index is None:
                self.separator(y, group_x, box_width)
                continue
            label, shortcut, _, enabled = items[index]
            attr = curses.A_REVERSE if index == self.menu_index else 0
            if not enabled:
                attr |= curses.A_DIM
            self.put(y, group_x + 1, " " * (box_width - 2), attr)
            self.put(y, group_x + 2, label, attr, max(0, box_width - len(shortcut) - 5))
            self.put(y, group_x + box_width - len(shortcut) - 2, shortcut, attr | curses.A_DIM)
            self.menu_hits.append((y, group_x, group_x + box_width, self.menu_group, index))

    def pick(self, title: str, items: list[Item], choose, *, kind="list", help=None) -> None:
        self.menu_open = False
        self.completion.clear()
        self.picker = Picker(title, items, choose, kind)
        if help:
            self.picker.help = help
        self.refresh_picker()

    def file_browser(self, *, importing=False, directory: Path | None = None) -> None:
        directory = Path(directory or self.source.path.parent).expanduser().resolve()
        try:
            entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        except OSError as error:
            self.status(str(error), True)
            return
        items = [Item("../", directory.parent)]
        items += [Item(p.name + ("/" if p.is_dir() else ""), p) for p in entries
                  if p.is_dir() and not p.name.startswith(".") or p.is_file() and p.suffix.lower() == ".opml"]

        def choose(item: Item) -> None:
            path = Path(item.value)
            if path.is_dir():
                self.file_browser(importing=importing, directory=path)
            elif importing:
                self.import_preview(path)
            else:
                self.switch_file(str(path))

        title = ("Import OPML" if importing else "Open OPML") + " · " + str(directory)
        self.pick(title, items, choose, kind="import-files" if importing else "files",
                  help="↑↓ choose · Enter open · Ctrl+L type path · Esc cancel")
        self.picker.directory = directory

    def current_index(self, editor=None, path=None) -> Index:
        editor, path = editor or self.editor, path or self.source.path
        key = (id(editor), editor.revision, str(path))
        if self._index_cache is None or self._index_cache[0] != key:
            self._index_cache = key, Index(editor.root, path)
        return self._index_cache[1]

    def open_search(self) -> None:
        self.pick("Search", [], self.choose_search, kind="search",
                  help="Tab scope · ↑↓ choose · Enter jump · Esc cancel")

    def search_items(self, query: str) -> list[Item]:
        e = self.preview.editor if self.preview else self.editor
        path = self.preview.source.path if self.preview else self.source.path
        indexes = [self.current_index(e, path)]
        errors = []
        if self.scope == "Global" and not self.preview:
            try:
                neighbours = sorted(path.parent.iterdir())
            except OSError as error:
                neighbours = []
                self.status(f"Could not read Global search directory: {error}", True)
            for other in neighbours:
                if other.suffix.lower() != ".opml" or other.name.lower() == "trash.opml" or other.resolve() == path:
                    continue
                try:
                    stat = other.stat()
                    key = stat.st_mtime_ns, stat.st_size
                    cached = self._global_cache.get(other)
                    if cached is None or cached[0] != key:
                        source, editor = OpmlFile.open(other)
                        cached = key, Index(editor.root, source.path)
                        self._global_cache[other] = cached
                    indexes.append(cached[1])
                except (OSError, ValueError):
                    errors.append(other.name)
            if errors:
                self.status("Skipped unreadable OPML: " + ", ".join(errors), True)
        allowed = {n.uid for n in e.zoom.walk()} if self.scope == "Current branch" and not self.preview else None
        hits = [h for index in indexes for h in index.search(query, allowed)]
        hits.sort(key=lambda h: h.score)
        return [Item(h.node.text or "(empty bullet)", h,
                     h.path.name + (" › " + h.breadcrumb if h.breadcrumb else "")) for h in hits]

    def choose_search(self, item: Item) -> None:
        hit: Hit = item.value
        if not self.preview and (self.closed or hit.path != self.source.path) and not hit.path.is_file():
            self.status("That search result's file is no longer available.", True)
            return

        def reveal() -> None:
            e = self.preview.editor if self.preview else self.editor
            target = next((n for n in e.root.walk() if n.uid == hit.node.uid), None)
            if target is None:
                # Global indexes hold an immutable source snapshot. Resolve its
                # branch by the full index path and attributes in the new file.
                trail = []
                node = hit.node
                while node.parent:
                    trail.append(node.parent.children.index(node))
                    node = node.parent
                target = e.root
                try:
                    for index in reversed(trail):
                        target = target.children[index]
                    if target.attrs != hit.node.attrs:
                        target = None
                except IndexError:
                    target = None
                if target is None:
                    candidates = [n for n in e.root.walk() if n.attrs == hit.node.attrs]
                    target = candidates[0] if len(candidates) == 1 else None
            if target is None:
                self.status("That result changed on disk. Search again.", True)
                return
            e.reveal(target)
            if hit.field == "_note":
                e.select(target, hit.positions[0] if hit.positions else 0, "_note")
            else:
                e.cursor = min(hit.positions[0] if hit.positions else 0, len(e.value))
            self.clear_selection()
            self.scroll = 0
            if self.preview:
                self.preview.scroll = 0

        if not self.preview and (self.closed or hit.path != self.source.path):
            self.switch_file(str(hit.path), reveal)
        else:
            reveal()

    def refresh_picker(self) -> None:
        p = self.picker
        if not p:
            return
        if p.kind == "search":
            p.matches = self.search_items(p.query)
        else:
            scored = [(fuzzy(p.query, item.title + " " + item.detail), i, item) for i, item in enumerate(p.items)]
            p.matches = [item for score, _, item in sorted((x for x in scored if x[0] is not None), key=lambda x: (x[0][0], x[1]))]
        p.selected = 0

    def picker_event(self, event: Event) -> None:
        p, key = self.picker, event.key
        if key == "escape":
            self.picker = None
            return
        if key in ("tab", "shift+tab") and p.kind == "search" and not self.preview:
            scopes = ("Current branch", "Whole file", "Global")
            self.scope = scopes[(scopes.index(self.scope) + (-1 if key == "shift+tab" else 1)) % 3]
            self.store.settings["search_scope"] = self.scope
            self.settings_changed()
            self.refresh_picker()
        elif key in ("up", "down", "pageup", "pagedown"):
            step = {"up": -1, "down": 1, "pageup": -8, "pagedown": 8}[key]
            p.selected = max(0, min(len(p.matches) - 1, p.selected + step))
        elif key == "enter":
            if p.kind in ("files", "import-files") and ("/" in p.query or not p.matches) and p.query.strip():
                path = Path(p.query).expanduser()
                if not path.is_absolute():
                    path = p.directory / path
                self.picker = None
                p.choose(Item(path.name, path))
            elif p.matches:
                item = p.matches[p.selected]
                self.picker = None
                p.choose(item)
        elif key == "ctrl+l" and p.kind in ("files", "import-files"):
            self.picker = None
            self.ask("OPML path", str(p.directory) + "/",
                     lambda path: p.choose(Item(path, Path(path).expanduser())), cancel=lambda: setattr(self, "picker", p))
        elif key == "ctrl+d" and p.kind == "stars" and p.matches:
            self.remove_star_item(p.matches[p.selected])
        elif key in ("left", "right", "home", "end"):
            p.cursor = {"left": max(0, p.cursor - 1), "right": min(len(p.query), p.cursor + 1),
                        "home": 0, "end": len(p.query)}[key]
        else:
            if key in ("text", "paste"):
                value = event.text.replace("\n", " ").replace("\r", " ")
                p.query = p.query[:p.cursor] + value + p.query[p.cursor:]
                p.cursor += len(value)
            elif key == "backspace" and p.cursor:
                p.query = p.query[:p.cursor - 1] + p.query[p.cursor:]
                p.cursor -= 1
            elif key == "delete":
                p.query = p.query[:p.cursor] + p.query[p.cursor + 1:]
            elif key == "ctrl+u":
                p.query, p.cursor = "", 0
            else:
                return
            self.refresh_picker()

    def draw_picker(self, height: int, columns: int):
        p = self.picker
        title = p.title + (" · Import source" if p.kind == "search" and self.preview else
                           " · " + self.scope if p.kind == "search" else "")
        y, x, h, w = self.panel(height, columns, title, 24)
        available = w - 6
        prefix = p.query[:p.cursor]
        start = 0
        while width(prefix[start:]) >= available:
            start += 1
        self.put(y + 2, x + 3, p.query[start:] or "Type to filter…", 0 if p.query else curses.A_DIM, available)
        self.separator(y + 3, x, w)
        capacity = max(1, (h - 8) // 2)
        first = max(0, p.selected - capacity + 1)
        for offset, item in enumerate(p.matches[first:first + capacity]):
            yy = y + 4 + offset * 2
            selected = first + offset == p.selected
            attr = curses.A_REVERSE if selected else 0
            self.put(yy, x + 2, " " * (w - 4), attr)
            self.put(yy, x + 3, item.title.replace("\n", " "), attr, available)
            self.put(yy + 1, x + 3, item.detail.replace("\n", " "), curses.A_DIM, available)
            hit = item.value
            if isinstance(hit, Hit) and hit.field == "text":
                for position in hit.positions:
                    if position < len(item.title):
                        xx = x + 3 + width(item.title[:position])
                        if xx < x + w - 3:
                            self.put(yy, xx, item.title[position], attr | curses.A_BOLD | self.color(3))
        if p.matches and isinstance(p.matches[p.selected].value, Hit):
            hit = p.matches[p.selected].value
            if hit.field == "_note":
                position = hit.positions[0] if hit.positions else 0
                excerpt = hit.node.note[max(0, position - 20):position + available].replace("\n", " ")
                self.put(y + h - 4, x + 3, "Note: " + excerpt, self.color(3), available)
        self.put(y + h - 3, x + 2, f"{len(p.matches)} results", curses.A_DIM, w - 4)
        self.put(y + h - 2, x + 2, p.help, curses.A_DIM, w - 4)
        return y + 2, x + 3 + width(prefix[start:])

    def fold(self, expanded: bool | None, recursive=False) -> None:
        if not self.editor.selected.children:
            self.status("Select a bullet with children to collapse or expand")
            return
        self.editor.toggle_fold(expanded, recursive)
        self.clear_selection()

    def toggle_completed(self) -> None:
        self.editor.show_completed = not self.editor.show_completed
        self.editor.ensure_selection()

    def toggle_star(self) -> None:
        node = self.editor.selected
        if node is self.editor.root:
            return
        if node.uid in self.editor.stars:
            self.editor.stars.remove(node.uid)
            self.status("Star removed")
        else:
            self.editor.stars.add(node.uid)
            self.status("Starred")
        self.persist_view()

    def star_browser(self) -> None:
        self.persist_view()
        current = references(self.editor, include_paths=False)
        records = [r for r in self.store.starred_files() if self.closed or r["file"] != str(self.source.path)]
        if not self.closed:
            records += [{"file": str(self.source.path), "ref": materialize(uid, current)}
                        for uid in self.editor.stars if uid in current]
            records += [{"file": str(self.source.path), "ref": ref} for ref in self.store.unresolved]
        items = [Item(r["ref"].get("label") or "(empty bullet)", r, Path(r["file"]).name) for r in records]

        def choose(item):
            record = item.value

            def jump():
                if any(r["uid"] == record["ref"].get("uid") for r in self.store.unresolved):
                    self.status("This bookmark is ambiguous after external edits; its record is preserved.", True)
                    return
                node = resolve(self.editor, record["ref"])
                if node:
                    self.editor.zoom_in(node)
                    self.scroll = 0
                else:
                    self.status("This starred node has changed or is unavailable.", True)
            if not self.closed and Path(record["file"]) == self.source.path:
                jump()
            elif not Path(record["file"]).is_file():
                self.status("Starred file is unavailable. Ctrl+D in Stars removes its bookmark.", True)
            else:
                self.switch_file(record["file"], jump)
        self.pick("Starred nodes", items, choose, kind="stars",
                  help="↑↓ choose · Enter open · Ctrl+D unstar · Esc cancel")

    def remove_star_item(self, item: Item) -> None:
        record = item.value
        uid = record["ref"]["uid"]
        path = Path(record["file"])
        try:
            if path == self.source.path and not self.closed:
                self.editor.stars.discard(uid)
                self.store.unresolved = [r for r in self.store.unresolved if r["uid"] != uid]
                self.persist_view()
            else:
                location = self.store.file_path(path)
                data = self.store.read(location)
                data["stars"] = [value for value in data.get("stars", []) if value != uid]
                self.store.write(location, data)
        except (OSError, ValueError) as error:
            self.status(f"Could not remove star: {error}", True)
            return
        self.star_browser()

    def label_names(self) -> list[str]:
        e = self.editor
        key = id(e), e.revision
        if self._label_cache is None or self._label_cache[0] != key:
            values = {}
            for node in e.root.walk():
                for _, _, label in label_spans(node.text + "\n" + node.note):
                    values.setdefault(label.casefold(), label)
            self._label_cache = key, sorted(values.values(), key=str.casefold)
        return self._label_cache[1]

    def label_browser(self) -> None:
        items = [Item(label, label, "mention" if label.startswith("@") else "hashtag") for label in self.label_names()]
        self.pick("Hashtags and mentions", items, lambda item: self.apply_label(item.value))

    def apply_label(self, label: str) -> None:
        self.editor.filter_edit_uid = None
        value = label.casefold()
        if value in self.editor.filter_labels:
            self.editor.filter_labels.remove(value)
        else:
            self.editor.filter_labels.add(value)
        self.editor.ensure_selection()
        self.scroll = 0
        self.clear_selection()

    def clear_filters(self) -> None:
        self.editor.filter_labels.clear()
        self.editor.filter_edit_uid = None
        self.status("Label filters cleared")

    def update_completion(self) -> None:
        self.completion.clear()
        e = self.editor
        match = re.search(r"(?<![\w/#@])([#@][\w/-]*)$", e.value[:e.cursor])
        if not match:
            return
        prefix = match[1]
        end = e.cursor
        while end < len(e.value) and (e.value[end].isalnum() or e.value[end] in "_-/"):
            end += 1
        self.completion = [label for label in self.label_names() if label.casefold().startswith(prefix.casefold())
                           and label != e.value[match.start(1):end]][:8]
        self.completion_index = 0
        self.completion_range = match.start(1), end

    def completion_event(self, event: Event) -> bool:
        if not self.completion:
            return False
        if event.key in ("up", "down"):
            self.completion_index = (self.completion_index + (-1 if event.key == "up" else 1)) % len(self.completion)
        elif event.key in ("tab", "enter"):
            value = self.completion[self.completion_index] + " "
            start, end = self.completion_range
            self.change(lambda: self.editor.replace(start, end, value))
            self.completion.clear()
        elif event.key == "escape":
            self.completion.clear()
        else:
            return False
        return True

    def request_delete(self, cut=False) -> None:
        node = self.editor.selected
        if node is self.editor.root:
            return
        count = sum(1 for _ in node.walk())

        def confirmed(choice):
            if choice != "y":
                return
            try:
                Trash(self.source.path).append(self.source.path, self.editor, node)
            except (OSError, ValueError) as error:
                self.status(f"Deletion cancelled; trash backup failed: {error}", True)
                return
            if cut:
                self.clipboard = Editor.clone(node)
            self.change(self.editor.delete_node)
            self.clear_selection()
            self.status(f"{'Cut' if cut else 'Deleted'} {count} bullets; backed up in trash.opml")

        self.ask("Delete: " + (node.text or "(empty bullet)")[:45],
                 f"{count} bullets → trash.opml · [y] confirm · [n] cancel", confirmed, choices=True)

    def trash_browser(self) -> None:
        trash = Trash(self.source.path)
        try:
            records = trash.records()
        except (OSError, ValueError) as error:
            self.status(f"Could not open Trash: {error}", True)
            return
        items = [Item(r.text, r, r.attrs.get("_trash_deleted", "") + " · "
                      + Path(r.attrs.get("_trash_source", "unknown")).name
                      + (" · restored before" if r.attrs.get("_trash_last_restored") else "")) for r in records]

        def choose(item):
            record = item.value
            options = [Item("Restore to original location", "original")]
            if not self.closed:
                options.append(Item("Restore here", "here"))
            self.pick("Restore: " + record.text[:45], options,
                      lambda option: self.restore_record(trash, record, option.value))
        self.pick("Trash / Restore", items, choose)

    def restore_record(self, trash: Trash, record: Node, placement: str) -> None:
        def restore():
            parent = index = None
            located = True
            if placement == "original":
                parent, index, located = Trash.original_parent(self.editor, record)
                self.editor.home()
            inserted = []

            def action():
                inserted.extend(self.editor.insert_trees(record.children, parent=parent, index=index))
            self.change(action)
            if inserted and record.attrs.get("_trash_starred") == "true":
                self.editor.stars.add(inserted[0].uid)
            if inserted:
                try:
                    for path in json.loads(record.attrs.get("_trash_stars", "[]")):
                        node = inserted[0]
                        for child_index in path:
                            node = node.children[child_index]
                        self.editor.stars.add(node.uid)
                except (ValueError, TypeError, IndexError):
                    pass
            try:
                trash.mark_restored(record.attrs["_trash_id"], self.source.path)
            except (OSError, ValueError) as error:
                self.status(f"Branch restored; could not annotate trash record: {error}", True)
                return
            self.scroll = 0
            self.status("Restored branch" if located else "Original parent unavailable; restored at document root")
        target = Path(record.attrs.get("_trash_source", str(self.source.path)))
        if placement == "original" and (self.closed or target != self.source.path):
            self.switch_file(str(target), restore)
        else:
            restore()

    def export_dialog(self, kind: str) -> None:
        e = self.editor
        options = [Item("Selected branch", e.selected), Item("Current zoomed branch", e.zoom), Item("Whole document", e.root)]

        def choose(item):
            content = render(e, item.value, kind, self.source.title)

            def filename(path):
                if not path.strip():
                    return
                target = Path(path).expanduser()
                if not target.suffix:
                    target = target.with_suffix("." + kind)
                if target.resolve() == self.source.path:
                    self.status("Choose an export filename different from the open document.", True)
                    return

                expected = None
                def write(overwrite=False):
                    try:
                        save_export(target, content, kind, overwrite=overwrite, expected=expected)
                    except (OSError, ValueError) as error:
                        self.status(f"Export failed: {error}", True)
                        return
                    self.status(f"Exported {target.name}")
                if target.exists():
                    try:
                        expected = digest(read_limited(target))
                    except (OSError, ValueError) as error:
                        self.status(f"Cannot read export destination: {error}", True)
                        return
                    self.ask("Replace exported file?", "[y] replace · [n] cancel",
                             lambda answer: write(True) if answer == "y" else None, choices=True)
                else:
                    write()
            self.ask("Export ." + kind + " filename", str(self.source.path.with_suffix("." + kind)), filename)
        self.pick("Export ." + kind + " · scope", options, choose)

    def import_preview(self, path: Path) -> None:
        try:
            source, imported = OpmlFile.open(path)
            if source.fingerprint is None:
                raise ValueError("Choose an existing OPML file to import")
        except (OSError, ValueError) as error:
            self.status(f"Import failed: {error}", True)
            return
        entire = Node({"text": "Entire file"})
        for child in list(imported.root.children):
            entire.add(child)
        root = Node(uid="root")
        root.add(entire)
        editor = Editor(root)
        editor.zoom_in(entire)

        def choose(node):
            trees = entire.children if node is entire else [node]
            count = sum(sum(1 for _ in tree.walk()) for tree in trees)
            preview = self.preview
            self.preview = None

            def commit(answer):
                if answer == "y":
                    self.change(lambda: self.editor.insert_trees(trees))
                    self.clear_selection()
                    self.scroll = 0
                    self.status(f"Imported {count} bullets from {path.name}")
                else:
                    self.preview = preview
            self.ask("Import selected tree?", f"{count} bullets · after selection / replace empty leaf · [y/n]",
                     commit, choices=True, cancel=lambda: setattr(self, "preview", preview))
        self.preview = Preview(source, editor, entire, "Import", choose)
        self.completion.clear()

    @contextmanager
    def preview_context(self):
        p = self.preview
        old = self.editor, self.rows, self.scroll, self.goal_column
        self.editor, self.rows, self.scroll, self.goal_column = p.editor, p.rows, p.scroll, None
        try:
            yield p
        finally:
            p.rows, p.scroll = self.rows, self.scroll
            self.editor, self.rows, self.scroll, self.goal_column = old

    def preview_event(self, event: Event) -> None:
        p = self.preview
        if event.key == "escape":
            self.preview = None
            return
        if event.key == "enter":
            p.choose(p.editor.selected)
            return
        if event.key in ("ctrl+f", "ctrl+k"):
            self.open_search()
            return
        with self.preview_context():
            if event.key in ("up", "down", "pageup", "pagedown"):
                step = {"up": -1, "down": 1, "pageup": -8, "pagedown": 8}[event.key]
                self.move_vertical(step)
            elif event.key in ("tab", "ctrl+space", "f9"):
                self.editor.toggle_fold()
            elif event.key in COLLAPSE_KEYS | EXPAND_KEYS:
                self.editor.toggle_fold(event.key in EXPAND_KEYS)
            elif event.key in ("alt+.", "alt+right", "f7"):
                self.editor.zoom_in()
            elif event.key in ("alt+,", "alt+left", "f8") and self.editor.zoom is not p.entire:
                self.editor.zoom_out()
            elif event.key == "f12":
                self.editor.zoom_in(p.entire)

    def draw_preview(self, height: int, columns: int):
        self.screen.erase()
        p = self.preview
        self.label_hits = []
        self.put(0, 2, "IMPORT PREVIEW · " + p.source.path.name, self.color(1) | curses.A_BOLD)
        with self.preview_context():
            self.draw_breadcrumbs(columns)
            self.build_rows(columns)
            current = self.cursor_row()
            viewport = height - 5
            self.scroll = max(0, min(self.scroll, current))
            if current >= self.scroll + viewport:
                self.scroll = current - viewport + 1
            for yy, row in enumerate(self.rows[self.scroll:self.scroll + viewport], 3):
                if row:
                    self.draw_row(yy, row, columns)
        count = sum(1 for _ in p.editor.selected.walk()) - (1 if p.editor.selected is p.entire else 0)
        self.put(height - 2, 2, f"{count} bullets selected · source is read-only", curses.A_DIM)
        self.put(height - 1, 2, "↑↓ select  Tab fold  F7/F8 zoom  ^F find  Enter choose tree  Esc cancel", curses.A_DIM)

    def draw_actions(self, height: int, columns: int, cursor):
        if self.closed:
            self.screen.erase()
            self.put(0, 2, "SPRIG", self.color(1) | curses.A_BOLD)
            self.put(3, 3, "No document open. Press Esc → File → Open, or F3.", curses.A_DIM)
            cursor = None
        if self.preview:
            self.draw_preview(height, columns)
            cursor = None
        if self.menu_open:
            self.draw_menu(columns)
            cursor = None
        if self.completion and not self.menu_open and not self.picker and cursor:
            yy = min(height - len(self.completion) - 2, cursor[0] + 1)
            xx = min(columns - 24, cursor[1])
            for index, label in enumerate(self.completion):
                attr = self.color(5 if label.startswith("@") else 1)
                if index == self.completion_index:
                    attr |= curses.A_REVERSE
                self.put(max(2, yy) + index, max(0, xx), label.ljust(22), attr, 24)
        if self.picker:
            cursor = self.draw_picker(height, columns)
        return cursor

    def handle_actions(self, event: Event) -> bool:
        if self.menu_open:
            if (event.key in ("f1", "f2", "f3", "f4", "f9", "f10", "f11", "ctrl+s", "ctrl+f", "ctrl+k", "ctrl+w", "ctrl+t", "alt+s", "ctrl+space")
                    or event.key in COLLAPSE_KEYS | EXPAND_KEYS):
                self.menu_open = False
            else:
                self.menu_event(event)
                return True
        if self.picker:
            self.picker_event(event)
            return True
        if self.preview:
            self.preview_event(event)
            return True
        if self.completion_event(event):
            return True
        key = event.key
        if key == "escape":
            self.menu_open = True
            self.completion.clear()
        elif key == "f3":
            self.file_browser()
        elif key == "ctrl+w":
            self.close_file()
        elif key in ("f11",):
            self.star_browser()
        elif not self.closed and key in ("ctrl+f", "ctrl+k"):
            self.open_search()
        elif not self.closed and key == "ctrl+t":
            self.label_browser()
        elif not self.closed and key == "alt+s":
            self.toggle_star()
        elif not self.closed and key in ("f10", "ctrl+shift+backspace"):
            self.request_delete()
        elif not self.closed and key in ("ctrl+alt+up", "ctrl+alt+down"):
            self.fold(key.endswith("down"), True)
        elif key in ("alt+left", "alt+right", "alt+b", "alt+f", "ctrl+alt+left", "ctrl+alt+right"):
            self.navigate_history(key in ("alt+right", "alt+f", "ctrl+alt+right"))
        elif not self.closed and key in ("ctrl+alt+m", "ctrl+alt+c"):
            self.transfer_picker(copy=key == "ctrl+alt+c")
        elif self.closed:
            return True
        else:
            return False
        return True
