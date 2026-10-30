"""An always-editable, zoomable outline rendered with standard-library curses."""

from dataclasses import dataclass
import curses
import os
from pathlib import Path
import re
import sys
import time
from typing import Callable

from . import richtext
from .keys import COLLAPSE_KEYS, EXPAND_KEYS, Event, Keyboard
from .layout import Segment, cell_width, clip, offset_at, printable, width, wrap
from .model import Editor, Node
from .opml import OpmlFile
from .search import label_spans
from .ui_actions import Actions
from .history import Location
from .storage import digest, read_limited


@dataclass
class Row:
    node: Node
    field: str
    segment: Segment
    x: int
    depth: int
    first: bool


@dataclass
class Prompt:
    label: str
    text: str
    cursor: int
    callback: Callable[[str], None]
    choices: bool = False
    cancel: Callable[[], None] | None = None


def clean_text(text: str) -> str:
    return "".join(c for c in text.replace("\r\n", "\n").replace("\r", "\n")
                   if c in "\t\n" or 32 <= ord(c) <= 0xd7ff
                   or 0xe000 <= ord(c) <= 0xfffd or 0x10000 <= ord(c) <= 0x10ffff)


class App(Actions):
    def __init__(self, screen, source: OpmlFile, editor: Editor, *,
                 autosave: bool | None = None, autosave_delay: float | None = None,
                 storage_options: dict | None = None) -> None:
        self.screen, self.source, self.editor = screen, source, editor
        self.autosave, self.autosave_delay = autosave, autosave_delay
        self.autosave_paused = False
        self.last_edit = time.monotonic()
        self.running = True
        self.scroll = 0
        self.rows: list[Row | None] = []
        self._rows_signature = None
        self._layout_root = None
        self._node_layouts = {}
        self.crumbs: list[tuple[int, int, Node]] = []
        self.anchor: int | None = None
        self.anchor_field = None
        self.goal_column: int | None = None
        self.clipboard: str | Node | None = None
        self.prompt: Prompt | None = None
        self.help_open = False
        self.help_scroll = 0
        self.message = ""
        self.message_error = False
        self.message_until = 0.0
        self.keyboard = Keyboard(screen)
        self.colors = False
        self.init_actions()
        self.init_storage({"autosave": autosave, "autosave_interval": autosave_delay, **(storage_options or {})})

    def setup(self) -> None:
        curses.raw()  # Ctrl+S/Q and Ctrl+Z belong to the editor, not the tty.
        self.screen.keypad(True)
        curses.noecho()
        try:
            curses.set_escdelay(25)
            curses.curs_set(1)
        except curses.error:
            pass
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                for index, color in enumerate((curses.COLOR_CYAN, curses.COLOR_GREEN,
                                               curses.COLOR_YELLOW, curses.COLOR_RED, curses.COLOR_MAGENTA), 1):
                    curses.init_pair(index, color, -1)
                self.colors = True
            except curses.error:
                pass
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
            curses.mouseinterval(0)
        except curses.error:
            pass
        # Terminals which support Kitty report Ctrl/Shift+Enter distinctly.
        # Other terminals can use the function-key equivalents.
        sys.stdout.write("\x1b[?2004h\x1b[>4;1m\x1b[>1u")
        sys.stdout.flush()

    def color(self, index: int) -> int:
        return curses.color_pair(index) if self.colors else curses.A_NORMAL

    def put(self, y: int, x: int, text: str, attr: int = 0, limit: int | None = None) -> None:
        height, columns = self.screen.getmaxyx()
        if not 0 <= y < height or not 0 <= x < columns:
            return
        available = columns - x - (1 if y == height - 1 else 0)
        if limit is not None:
            available = min(available, max(0, limit))
        try:
            self.screen.addstr(y, x, clip(text, available), attr)
        except curses.error:
            pass

    def status(self, message: str, error: bool = False) -> None:
        self.message, self.message_error = message, error
        self.message_until = time.monotonic() + (15 if error else 4)
        if error and getattr(self, "debug", None) is not None:
            self.debug.event("status.error")

    def selection(self) -> tuple[int, int] | None:
        e = self.editor
        if self.anchor is not None and self.anchor_field == (e.selected.uid, e.field) and self.anchor != e.cursor:
            return min(self.anchor, e.cursor), max(self.anchor, e.cursor)
        return None

    def clear_selection(self) -> None:
        self.anchor = None
        self.anchor_field = None

    def change(self, action: Callable[[], object], group: str | None = None) -> None:
        if self.editor.change(action, group):
            self.last_edit = time.monotonic()
        self.goal_column = None

    def build_rows(self, columns: int) -> None:
        e = self.editor
        visible = e.visible()
        selected_index = next((i for i, (n, _) in enumerate(visible) if n is e.selected), 0)
        selected_depth = visible[selected_index][1] if visible else 0
        # Layout a window around the caret. Navigation still uses the whole
        # logical outline, but opening a large account doesn't wrap every note.
        window_start = max(0, (selected_index // 64 - 1) * 64)
        window_end = min(len(visible), window_start + 192)
        visible = visible[window_start:window_end]
        margin = max(2, (columns - 104) // 2)
        usable = min(columns - margin - 2, 104)
        max_depth = max(1, (usable - 28) // 2)
        self.indent_offset = max(0, selected_depth - max_depth)
        empty_note = e.selected.uid if e.field == "_note" and not e.selected.note else None
        signature = (id(e.root), e.revision, e.zoom.uid, columns, self.indent_offset,
                     frozenset(e.collapsed), e.show_completed, frozenset(e.filter_labels), empty_note,
                     e.filter_edit_uid, window_start, window_end)
        if signature == self._rows_signature:
            return
        self._rows_signature = signature
        anchor = self.rows[self.scroll] if self.rows and 0 <= self.scroll < len(self.rows) else None
        if self._layout_root != id(e.root):
            self._node_layouts.clear()
            self._layout_root = id(e.root)
        self.rows = []
        if not visible:
            visible = [(e.root, 0)]
        for node, depth in visible:
            x = margin if depth == -1 else margin + 4 + min(max_depth, max(0, depth - self.indent_offset)) * 2
            layout_key = id(node), columns, x, depth, node.attrs.get("text", ""), node.attrs.get("_note", ""), node.uid == empty_note
            previous = self._node_layouts.get(node.uid)
            if previous is not None and previous[0] == layout_key:
                self.rows.extend(previous[1])
                continue
            node_rows = []
            fields = ["text"]
            if node.note or (node is e.selected and e.field == "_note"):
                fields.append("_note")
            for field in fields:
                text = richtext.plain(node.attrs.get(field, "")) if node is not e.root else ""
                field_x = x + (2 if field == "_note" else 0)
                segments = wrap(text, max(4, min(columns - field_x - 2, usable - (field_x - margin))))
                for index, segment in enumerate(segments):
                    node_rows.append(Row(node, field, segment, field_x, depth, index == 0))
            if depth == -1:
                node_rows.append(None)
            self._node_layouts[node.uid] = layout_key, node_rows
            self.rows.extend(node_rows)
        if anchor is not None:
            self.scroll = next((i for i, row in enumerate(self.rows) if row is anchor), 0)

    def cursor_row(self) -> int:
        e = self.editor
        candidate = 0
        for index, row in enumerate(self.rows):
            if row and row.node is e.selected and row.field == e.field and row.segment.start <= e.cursor:
                candidate = index
                if e.cursor < row.segment.end:
                    break
        return candidate

    def draw_breadcrumbs(self, columns: int) -> None:
        e = self.editor
        nodes = [e.root]
        if e.zoom is not e.root:
            nodes += [n for n in reversed(list(e.ancestors(e.zoom))) if n is not e.root]
            nodes.append(e.zoom)
        labels = ["Home" if n is e.root else clip(n.text or "Untitled", 20) for n in nodes]
        items = list(zip(nodes, labels))
        if sum(width(label) + 3 for _, label in items) > columns - 4 and len(items) > 2:
            # Retain the nearest ancestors in one pass. Repeatedly measuring
            # the whole path makes editing very deep zoomed bullets quadratic.
            remaining = max(4, columns - 4 - width("Home") - width(" › … › "))
            suffix = []
            for node, label in reversed(items[1:]):
                cost = width(label) + (3 if suffix else 0)
                if suffix and cost > remaining:
                    break
                if cost > remaining:
                    label = clip(label, remaining)
                    cost = width(label)
                suffix.append((node, label))
                remaining -= cost
            items = [items[0], *reversed(suffix)]
        self.crumbs = []
        x = 2
        for index, (node, label) in enumerate(items):
            if index:
                separator = " › … › " if index == 1 and len(items) < len(nodes) else " › "
                self.put(1, x, separator, curses.A_DIM)
                x += width(separator)
            self.put(1, x, label, self.color(1) if node is e.zoom else curses.A_DIM)
            self.crumbs.append((x, x + width(label), node))
            x += width(label)

    def draw_row(self, y: int, row: Row, columns: int) -> None:
        e = self.editor
        node = row.node
        active = node is e.selected
        note = row.field == "_note"
        base = curses.A_DIM if note or node.completed else curses.A_NORMAL
        if row.depth == -1 and not note:
            base |= curses.A_BOLD
        if row.first and not note and row.depth >= 0:
            marker = "✓" if node.completed else "•"
            self.put(y, row.x - 2, marker, self.color(1) if active else curses.A_DIM)
            if node.children:
                self.put(y, row.x - 4, "▸" if node.uid in e.collapsed else "▾", curses.A_DIM)
            if self.indent_offset and row.depth >= self.indent_offset:
                self.put(y, max(0, row.x - 6), "‹", curses.A_DIM)
        raw = node.attrs.get(row.field, "") if node is not e.root else ""
        text = richtext.plain(raw)
        segment = row.segment
        styles = [t.style for t in richtext.tokens(raw) for _ in t.text]
        selected = self.selection() if active and e.field == row.field else None
        tags = label_spans(text)
        for start, end, label in tags:
            left, right = max(start, segment.start), min(end, segment.end)
            if right > left:
                xx = row.x + width(text[segment.start:left])
                self.label_hits.append((y, xx, xx + width(text[left:right]), label))
        x = row.x
        for position in range(segment.start, segment.end):
            char = text[position]
            attr = base
            style = styles[position] if position < len(styles) else frozenset()
            if "b" in style:
                attr |= curses.A_BOLD
            if "u" in style or "a" in style:
                attr |= curses.A_UNDERLINE
            if "i" in style:
                attr |= getattr(curses, "A_ITALIC", curses.A_DIM)
            if "s" in style:
                attr |= curses.A_DIM
            for start, end, label in tags:
                if start <= position < end:
                    attr |= self.color(5 if label.startswith("@") else 1)
                    break
            if selected and selected[0] <= position < selected[1]:
                attr |= curses.A_REVERSE
            self.put(y, x, printable(char), attr)
            x += cell_width(char)
        if not text and row.first and active:
            self.put(y, row.x, "Add a note…" if note else "New bullet", curses.A_DIM)
        if row.first and not note and node.uid in e.stars:
            self.put(y, x + 1, "★", self.color(3))
            x += 2
        if row.first and not note and node.uid in e.collapsed and node.children:
            count = sum(1 for _ in node.walk()) - 1
            self.put(y, x + 1, f" · {count}", curses.A_DIM, columns - x - 3)

    def draw(self) -> None:
        self.screen.erase()
        self.label_hits = []
        height, columns = self.screen.getmaxyx()
        if height < 8 or columns < 30:
            self.put(0, 0, "Please resize to at least 30 × 8. Ctrl+Q quits.")
            self.screen.refresh()
            return
        if self.preview:
            self.draw_preview(height, columns)
            cursor = self.draw_picker(height, columns) if self.picker else None
            if self.prompt:
                cursor = self.draw_prompt(height, columns)
            self.finish_frame(cursor)
            return
        e = self.editor
        e.ensure_selection()
        self.put(0, 2, "SPRIG", self.color(1) | curses.A_BOLD)
        state = "Save failed" if self.autosave_paused else ("Unsaved" if e.dirty else "Saved")
        if self.source.fingerprint is None and not e.dirty:
            state = "New file"
        state += " · snapshots" if self.autosave else " · manual"
        self.put(0, max(12, columns - width(state) - 2), state,
                 self.color(4 if self.autosave_paused else 3 if e.dirty else 2))
        self.put(0, 12, self.source.path.name, curses.A_DIM, columns - width(state) - 16)
        self.draw_breadcrumbs(columns)
        if e.filter_labels:
            self.put(2, 2, "Filter: " + "  ".join(sorted(e.filter_labels)) + "  · Navigate → Clear label filters", self.color(1))
        loading = self._layout_root != id(e.root)
        if loading:
            self.put(3, 2, "Laying out outline…", curses.A_DIM)
            self.screen.refresh()
        self.build_rows(columns)
        if loading:
            self.put(3, 0, " " * columns)
        current = self.cursor_row()
        viewport = height - 5
        if current < self.scroll:
            self.scroll = current
        elif current >= self.scroll + viewport:
            self.scroll = current - viewport + 1
        self.scroll = min(self.scroll, max(0, len(self.rows) - viewport))
        for index, row in enumerate(self.rows[self.scroll:self.scroll + viewport], 3):
            if row:
                self.draw_row(index, row, columns)
        total = sum(1 for _ in e.root.walk()) - 1
        depth = sum(1 for _ in e.ancestors(e.selected))
        detail = f"{total} bullets · depth {depth}" + (" · note" if e.field == "_note" else "")
        if not e.show_completed:
            detail += " · completed hidden"
        if time.monotonic() < self.message_until:
            self.put(height - 2, 2, self.message, self.color(4) if self.message_error else self.color(2))
        else:
            self.put(height - 2, 2, detail, curses.A_DIM)
        self.put(height - 1, 2, self.keymap.footer(), curses.A_DIM)
        row = self.rows[current] if self.rows else None
        cursor = None
        if row:
            text = e.value if row.node is e.selected else ""
            cursor = (3 + current - self.scroll, row.x + width(text[row.segment.start:e.cursor]))
        if self.help_open:
            self.draw_help(height, columns)
            cursor = None
        else:
            cursor = self.draw_actions(height, columns, cursor)
            if self.prompt:
                cursor = self.draw_prompt(height, columns)
        self.finish_frame(cursor)

    def finish_frame(self, cursor) -> None:
        height, columns = self.screen.getmaxyx()
        try:
            curses.curs_set(1 if cursor else 0)
            if cursor:
                self.screen.move(max(0, min(height - 1, cursor[0])), max(0, min(columns - 2, cursor[1])))
        except curses.error:
            pass
        self.screen.refresh()

    def box(self, y: int, x: int, h: int, w: int) -> None:
        for row in range(h):
            self.put(y + row, x, " " * w)
            self.put(y + row, x, "║", curses.A_DIM)
            self.put(y + row, x + w - 1, "║", curses.A_DIM)
        self.put(y, x, "╔" + "═" * (w - 2) + "╗", curses.A_DIM)
        self.put(y + h - 1, x, "╚" + "═" * (w - 2) + "╝", curses.A_DIM)

    def separator(self, y: int, x: int, w: int) -> None:
        self.put(y, x, "╟" + "─" * (w - 2) + "╢", curses.A_DIM)

    def panel(self, height: int, columns: int, title: str, wanted: int) -> tuple[int, int, int, int]:
        h, w = min(height - 2, wanted), min(columns - 4, 84)
        y, x = max(1, (height - h) // 2), max(1, (columns - w) // 2)
        self.box(y, x, h, w)
        self.put(y, x + 2, " " + title + " ", self.color(1) | curses.A_BOLD, w - 4)
        return y, x, h, w

    def draw_help(self, height: int, columns: int) -> None:
        y, x, h, w = self.panel(height, columns, "Keyboard shortcuts", 48)
        lines = self.keymap.help_lines()
        self.help_scroll = max(0, min(self.help_scroll, len(lines) - (h - 2)))
        for index, line in enumerate(lines[self.help_scroll:self.help_scroll + h - 2], 1):
            self.put(y + index, x + 2, line, limit=w - 4)

    def draw_prompt(self, height: int, columns: int) -> tuple[int, int] | None:
        p = self.prompt
        y, x, h, w = self.panel(height, columns, p.label, 7)
        if p.choices:
            self.put(y + 2, x + 3, p.text, limit=w - 6)
            self.put(y + h - 2, x + 3, "Press a choice key · Esc cancels", curses.A_DIM, w - 6)
            return None
        prefix = p.text[:p.cursor]
        start = 0
        while width(prefix[start:]) > w - 8:
            start += 1
        self.put(y + 2, x + 3, p.text[start:], limit=w - 6)
        self.put(y + h - 2, x + 3, "Enter accepts · Esc cancels", curses.A_DIM, w - 6)
        return y + 2, x + 3 + width(prefix[start:])

    def move_vertical(self, direction: int) -> None:
        if not self.rows:
            return
        e = self.editor
        current = self.cursor_row()
        row = self.rows[current]
        if row is None:
            return
        if self.goal_column is None:
            self.goal_column = width(e.value[row.segment.start:e.cursor])
        target = max(0, min(len(self.rows) - 1, current + direction))
        step = 1 if direction > 0 else -1
        while 0 <= target < len(self.rows) and self.rows[target] is None:
            target += step
        if not 0 <= target < len(self.rows):
            return
        row = self.rows[target]
        text = richtext.plain(row.node.attrs.get(row.field, ""))
        position = offset_at(text, row.segment.start, row.segment.end, self.goal_column)
        e.select(row.node, position, row.field)
        self.clear_selection()

    def move_horizontal(self, direction: int, *, word: bool = False, extend: bool = False) -> None:
        e = self.editor
        if extend and self.anchor is None:
            self.anchor, self.anchor_field = e.cursor, (e.selected.uid, e.field)
        if not extend:
            selected = self.selection()
            if selected and not word:
                e.cursor = selected[0 if direction < 0 else 1]
                self.clear_selection()
                return
            self.clear_selection()
        position = e.cursor + direction
        if word and direction < 0:
            match = re.search(r"(?:\w+|[^\w\s]+)\s*$", e.value[:e.cursor])
            position = match.start() if match else 0
        elif word:
            match = re.match(r"\s*(?:\w+|[^\w\s]+)\s*", e.value[e.cursor:])
            position = e.cursor + len(match[0]) if match else len(e.value)
        if 0 <= position <= len(e.value) or extend:
            e.cursor = max(0, min(position, len(e.value)))
        else:
            current = self.cursor_row()
            target = current + direction
            while 0 <= target < len(self.rows):
                row = self.rows[target]
                if row:
                    e.select(row.node, row.segment.end if direction < 0 else row.segment.start, row.field)
                    break
                target += direction
        e.break_group()
        self.goal_column = None

    def insert_text(self, text: str, *, paste: bool = False) -> None:
        e = self.editor
        text = clean_text(text)
        selected = self.selection()
        if selected:
            self.change(lambda: e.replace(*selected, text))
        elif paste:
            self.change(lambda: e.paste_outline(text))
        else:
            self.change(lambda: e.insert(text), "typing")
        self.clear_selection()
        if not paste:
            self.update_completion()

    def note(self) -> None:
        e = self.editor
        if e.selected is e.root:
            return
        field = "text" if e.field == "_note" else "_note"
        e.select(e.selected, len(e.selected.text if field == "text" else e.selected.note), field)
        self.clear_selection()

    def copy(self, cut: bool = False) -> None:
        e = self.editor
        selected = self.selection()
        if cut and not selected:
            self.request_delete(cut=True)
            return
        if selected:
            self.clipboard = e.value[selected[0]:selected[1]]
            if cut:
                self.change(lambda: e.replace(*selected, ""))
                self.clear_selection()
        elif e.selected is not e.root:
            self.clipboard = e.clone(e.selected)
        self.status("Cut to internal clipboard" if cut else "Copied to internal clipboard")

    def paste(self) -> None:
        if isinstance(self.clipboard, str):
            self.insert_text(self.clipboard)
        elif self.clipboard is not None:
            e = self.editor

            def action() -> None:
                node = e.clone(self.clipboard)
                current = e.selected
                if current is e.zoom:
                    current.add(node, 0)
                else:
                    current.parent.add(node, current.parent.children.index(current) + 1)
                e.select(node)

            self.change(action)
            self.clear_selection()
        else:
            self.status("Internal clipboard empty. Use your terminal's paste shortcut for external text.")

    def format(self, tag: str) -> None:
        e = self.editor
        if e.selected is e.root or not e.value:
            return
        selected = self.selection()

        def action() -> None:
            raw = e.selected.attrs.get(e.field, "")
            if selected:
                left, right = richtext.split(raw, selected[1])
                before, middle = richtext.split(left, selected[0])
                value = before + f"<{tag}>" + middle + f"</{tag}>" + right
            elif raw.startswith(f"<{tag}>") and raw.endswith(f"</{tag}>"):
                value = raw[len(tag) + 2:-len(tag) - 3]
            else:
                value = f"<{tag}>" + raw + f"</{tag}>"
            e.selected.attrs[e.field] = value

        self.change(action)

    def save(self, path: str | None = None, *, overwrite: bool = False, expected: str | None = None) -> bool:
        if self.closed:
            self.status("No document open", True)
            return False
        self.checkpoint()
        try:
            self.source.save(self.editor, path, overwrite=overwrite, expected=expected)
        except (OSError, ValueError) as error:
            self.debug.exception("save", error)
            self.autosave_paused = True
            self.status(f"Save failed: {error}", True)
            return False
        self.autosave_paused = False
        self.recovered = False
        if not self.source.warning:
            self.clear_recovery()
        self.status(self.source.warning or f"Saved {self.source.path.name}", bool(self.source.warning))
        self.store.capture(self.editor)
        self.persist_view()
        return True

    def save_as(self, path: str) -> None:
        if not path.strip():
            return
        target = Path(path).expanduser()
        if not target.suffix:
            target = target.with_suffix(".opml")
        if target.resolve() != self.source.path and target.exists():
            try:
                expected = digest(read_limited(target))
            except (OSError, ValueError) as error:
                self.status(str(error), True)
                return
            self.prompt = Prompt("Replace existing file?", "[y] replace · [n] cancel", 0,
                                 lambda choice: self.save(str(target), overwrite=True, expected=expected) if choice == "y" else None, True)
        else:
            self.save(str(target))

    def guard_changes(self, action: Callable[[], None]) -> None:
        if not self.editor.dirty:
            action()
            return
        if self.save_on_exit and not self.autosave_paused and self.save():
            action()
            return

        def choose(choice: str) -> None:
            if choice == "d" or (choice == "s" and self.save()):
                self._discarding = choice == "d"
                try:
                    action()
                finally:
                    self._discarding = False

        self.prompt = Prompt("Unsaved changes", "[s] save · [d] discard · [Esc] cancel", 0, choose, True)

    def open_file(self, path: str) -> None:
        self.switch_file(path)

    def prompt_event(self, event: Event) -> None:
        p, key = self.prompt, event.key
        if key == "escape":
            self.prompt = None
            if p.cancel:
                p.cancel()
        elif p.choices:
            if key == "text" and event.text.lower() in "ysdn":
                self.prompt = None
                p.callback(event.text.lower())
        elif key == "enter":
            self.prompt = None
            p.callback(p.text)
        elif key in ("text", "paste"):
            value = clean_text(event.text).replace("\n", "")
            p.text = p.text[:p.cursor] + value + p.text[p.cursor:]
            p.cursor += len(value)
        elif key == "backspace" and p.cursor:
            p.text = p.text[:p.cursor - 1] + p.text[p.cursor:]
            p.cursor -= 1
        elif key == "delete":
            p.text = p.text[:p.cursor] + p.text[p.cursor + 1:]
        elif key == "left":
            p.cursor = max(0, p.cursor - 1)
        elif key == "right":
            p.cursor = min(len(p.text), p.cursor + 1)
        elif key in ("home", "ctrl+a"):
            p.cursor = 0
        elif key in ("end", "ctrl+e"):
            p.cursor = len(p.text)
        elif key == "ctrl+u":
            p.text, p.cursor = "", 0

    def mouse(self) -> None:
        try:
            _, x, y, _, state = curses.getmouse()
        except curses.error:
            return
        if state & curses.BUTTON4_PRESSED:
            self.move_vertical(-3)
            return
        if state & getattr(curses, "BUTTON5_PRESSED", 0):
            self.move_vertical(3)
            return
        if not state & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED):
            return
        for yy, left, right, label in self.label_hits:
            if yy == y and left <= x < right:
                self.apply_label(label)
                return
        if y == 1:
            for left, right, node in self.crumbs:
                if left <= x < right:
                    self.editor.zoom = node
                    self.editor.select(node if node is not self.editor.root else self.editor.root.children[0]
                                       if self.editor.root.children else node)
                    self.scroll = 0
                    self.clear_selection()
                    return
        index = y - 3 + self.scroll
        if y < 3 or y >= self.screen.getmaxyx()[0] - 2 or not 0 <= index < len(self.rows):
            return
        row = self.rows[index]
        if not row:
            return
        e = self.editor
        if row.first and row.field == "text" and row.depth >= 0 and row.x - 4 <= x < row.x - 2:
            e.select(row.node)
            e.toggle_fold()
        elif row.first and row.field == "text" and row.depth >= 0 and row.x - 2 <= x < row.x:
            e.zoom_in(row.node)
            self.scroll = 0
        else:
            text = richtext.plain(row.node.attrs.get(row.field, ""))
            position = offset_at(text, row.segment.start, row.segment.end, max(0, x - row.x))
            e.select(row.node, position, row.field)
        self.clear_selection()

    def handle(self, event: Event) -> None:
        started = time.monotonic()
        if self.debug.path is not None:
            safe_input = "[input redacted]" if event.key in ("text", "paste") else self.keyboard.last_sequence
            if not self.debug.event("input", key=event.key, sequence=safe_input, command=self.keymap.command(event),
                                    field=self.editor.field, has_children=bool(self.editor.selected.children),
                                    filtered=bool(self.editor.filter_labels), menu=self.menu_open,
                                    picker=bool(self.picker), prompt=bool(self.prompt)):
                self.status("Debug logging stopped: " + self.debug.failure, True)
        command = self.keymap.command(event)
        if command == "quit":
            event = Event("ctrl+q")
        elif not (self.prompt or self.picker or self.help_open or self.menu_open):
            event = self.keymap.translate(event)
        elif event.key == "ctrl+q":
            event = Event("unbound")
        elif self.help_open and command == "help":
            event = Event("f1")
        elif self.menu_open and command in {"save", "save_as", "open", "close", "search", "stars", "labels",
                                           "star", "delete", "help", "collapse", "expand", "toggle_fold"}:
            event = self.keymap.translate(event)
        track = not self.preview and (event.key in ("f7", "f8", "f12", "alt+.", "alt+,", "mouse")
                                     or (event.key == "enter" and (self.picker is not None or self.menu_open)))
        before = Location.capture(self) if track else None
        version = id(self.editor), self.editor.revision
        self._navigated_history = False
        self._handle_event(event)
        if self.debug.path is not None:
            if not self.debug.event("input.done", key=event.key, revision=self.editor.revision,
                                    collapsed=len(self.editor.collapsed), elapsed_ms=round((time.monotonic() - started) * 1000, 2)):
                self.status("Debug logging stopped: " + self.debug.failure, True)
        if (before and not self._navigated_history
                and ((id(self.editor), self.editor.revision) == version or self.source.path != before.path)):
            self.history.record(before, Location.capture(self))

    def _handle_event(self, event: Event) -> None:
        e, key = self.editor, event.key
        if key == "resize":
            self.screen.clear()
            return
        if key == "ctrl+q":
            self.help_open = False
            self.prompt = None
            self.menu_open = False
            self.picker = self.preview = None
            self.completion.clear()
            def quit_app():
                self.persist_view()
                self.exit_discarded = self._discarding
                if self._discarding or (not self.editor.dirty and not self.source.warning):
                    self.clear_recovery()
                self.running = False
            self.guard_changes(quit_app)
            return
        if self.help_open:
            if key in ("escape", "f1", "ctrl+/", "q") or (key == "text" and event.text == "q"):
                self.help_open = False
            elif key in ("up", "down", "pageup", "pagedown", "home", "end"):
                step = {"up": -1, "down": 1, "pageup": -12, "pagedown": 12,
                        "home": -len(self.keymap.help_lines()), "end": len(self.keymap.help_lines())}[key]
                self.help_scroll += step
            return
        if self.prompt:
            self.prompt_event(event)
            return
        if self.handle_actions(event):
            return
        if key == "text":
            self.insert_text(event.text)
            return
        if key == "paste":
            self.insert_text(event.text, paste=True)
            return
        if key == "mouse":
            self.mouse()
            return
        if key in ("up", "down", "pageup", "pagedown"):
            page = max(1, self.screen.getmaxyx()[0] - 7)
            self.move_vertical({"up": -1, "down": 1, "pageup": -page, "pagedown": page}[key])
        elif key in ("left", "right", "ctrl+left", "ctrl+right", "shift+left", "shift+right"):
            self.move_horizontal(-1 if key.endswith("left") else 1,
                                 word=key.startswith("ctrl"), extend=key.startswith("shift"))
        elif key in ("home", "end", "shift+home", "shift+end", "ctrl+home", "ctrl+end"):
            if key.startswith("shift") and self.anchor is None:
                self.anchor, self.anchor_field = e.cursor, (e.selected.uid, e.field)
            elif not key.startswith("shift"):
                self.clear_selection()
            if key.startswith("ctrl"):
                visible = e.visible()
                if visible:
                    node = visible[0 if key.endswith("home") else -1][0]
                    field = "_note" if key.endswith("end") and node.note else "text"
                    value = node.note if field == "_note" else node.text
                    e.select(node, 0 if key.endswith("home") else len(value), field)
            elif self.rows:
                row = self.rows[self.cursor_row()]
                if row:
                    e.cursor = row.segment.start if key.endswith("home") else row.segment.end
            e.break_group()
            self.goal_column = None
        elif key == "ctrl+a":
            self.anchor, self.anchor_field = 0, (e.selected.uid, e.field)
            e.cursor = len(e.value)
        elif key in ("backspace", "delete", "ctrl+backspace", "alt+backspace", "ctrl+w"):
            selected = self.selection()
            if selected:
                self.change(lambda: e.replace(*selected, ""))
            elif key in ("ctrl+backspace", "alt+backspace", "ctrl+w"):
                end = e.cursor
                self.move_horizontal(-1, word=True)
                start = e.cursor
                self.change(lambda: e.replace(start, end, ""))
            elif key == "backspace":
                self.change(e.backspace, "backspace")
            elif e.cursor < len(e.value):
                self.change(lambda: e.replace(e.cursor, e.cursor + 1, ""), "delete")
            self.clear_selection()
        elif key == "enter":
            selected = self.selection()

            def enter() -> None:
                if selected:
                    e.replace(*selected, "")
                e.enter()

            self.change(enter)
            self.clear_selection()
        elif key in ("tab", "shift+tab", "alt+shift+right", "alt+shift+left"):
            self.change(e.indent if key in ("tab", "alt+shift+right") else e.outdent)
            self.clear_selection()
        elif key in ("alt+.", "f7", "alt+,", "f8"):
            e.zoom_in() if key in ("alt+.", "f7") else e.zoom_out()
            self.scroll = 0
            self.clear_selection()
        elif key == "f12":
            e.home()
            self.clear_selection()
        elif key in COLLAPSE_KEYS | EXPAND_KEYS | {"ctrl+space", "f9"}:
            self.fold(False if key in COLLAPSE_KEYS else True if key in EXPAND_KEYS else None)
        elif key in ("alt+up", "alt+shift+up", "ctrl+shift+up", "alt+down", "alt+shift+down", "ctrl+shift+down"):
            self.change(lambda: e.move(-1 if key.endswith("up") else 1))
        elif key in ("shift+enter", "f6"):
            self.note()
        elif key in ("ctrl+enter", "f5"):
            self.change(e.toggle_complete)
            self.clear_selection()
        elif key == "ctrl+o":
            e.show_completed = not e.show_completed
            e.ensure_selection()
            self.clear_selection()
        elif key in ("ctrl+d", "alt+d", "alt+shift+d"):
            self.change(e.duplicate)
            self.clear_selection()
        elif key in ("ctrl+c", "ctrl+x"):
            self.copy(key == "ctrl+x")
        elif key == "ctrl+v":
            self.paste()
        elif key in ("ctrl+b", "alt+i", "ctrl+u"):
            self.format({"ctrl+b": "b", "alt+i": "i", "ctrl+u": "u"}[key])
        elif key in ("ctrl+z", "ctrl+y", "ctrl+shift+z"):
            changed = e.undo() if key == "ctrl+z" else e.redo()
            if changed:
                self.last_edit = time.monotonic()
            self.clear_selection()
        elif key in ("ctrl+s", "f2"):
            self.save()
        elif key == "f4":
            text = str(self.source.path)
            self.prompt = Prompt("Save as", text, len(text), self.save_as)
        elif key in ("f1", "ctrl+/"):
            self.help_open = True
        elif key == "ctrl+l":
            self.screen.clear()
        if key in ("left", "right", "ctrl+left", "ctrl+right", "backspace", "delete", "ctrl+backspace", "alt+backspace"):
            self.update_completion()
        else:
            self.completion.clear()

    def run(self) -> None:
        try:
            self.setup()
            self.debug.event("session.start", python=sys.version.split()[0], platform=sys.platform,
                             term=os.environ.get("TERM"), terminal_program=os.environ.get("TERM_PROGRAM"),
                             size=list(self.screen.getmaxyx()), bindings=len(self.keymap.commands))
            self.offer_recovery()
            self.draw()
            while self.running:
                event = self.keyboard.read()
                redraw = event is not None
                if event:
                    self.handle(event)
                redraw = self.storage_tick() or redraw
                if redraw and self.running:
                    self.draw()
        except BaseException as error:
            self.debug.exception("editor.run", error)
            raise
        finally:
            self.checkpoint()
            self.persist_view()
            self.recovery.close()
            self.debug.event("session.end", unsaved=self.editor.dirty, discarded=self.exit_discarded)
            sys.stdout.write("\x1b[<u\x1b[>4m\x1b[?2004l")
            sys.stdout.flush()
