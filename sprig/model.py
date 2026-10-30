"""Tree operations and undo history, independent of terminal and file I/O."""

from dataclasses import dataclass, field
import time
from typing import Callable, Iterator
from uuid import uuid4
from xml.etree.ElementTree import Element

from . import richtext


@dataclass(eq=False)
class Node:
    attrs: dict[str, str] = field(default_factory=lambda: {"text": ""})
    children: list["Node"] = field(default_factory=list)
    parent: "Node | None" = field(default=None, repr=False)
    uid: str = field(default_factory=lambda: uuid4().hex)
    extras: tuple[tuple[int, Element], ...] = ()
    _record: tuple | None = field(default=None, init=False, repr=False)

    def record(self) -> tuple:
        value = (self.uid, self.parent.uid if self.parent else None, tuple(self.attrs.items()), self.extras)
        if self._record != value:
            self._record = value
        return self._record

    @property
    def text(self) -> str:
        return richtext.plain(self.attrs.get("text", ""))

    @property
    def note(self) -> str:
        return richtext.plain(self.attrs.get("_note", ""))

    @property
    def completed(self) -> bool:
        return self.attrs.get("_complete", "").lower() in ("true", "1", "yes")

    def add(self, node: "Node", index: int | None = None) -> None:
        parent = self
        while parent is not None:
            if parent is node:
                raise ValueError("A node cannot be placed inside itself or its descendants")
            parent = parent.parent
        if node.parent is not None and node in node.parent.children:
            node.parent.children.remove(node)
        node.parent = self
        self.children.insert(len(self.children) if index is None else index, node)

    def walk(self) -> Iterator["Node"]:
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))


@dataclass(frozen=True)
class Snapshot:
    records: tuple
    selected: str
    zoom: str
    cursor: int
    field: str
    collapsed: frozenset[str]
    filters: frozenset[str]
    filter_edit_uid: str | None


class Editor:
    def __init__(self, root: Node | None = None) -> None:
        self.root = root if root is not None else Node(uid="root")
        self.zoom = self.root
        self.selected = self.root.children[0] if self.root.children else self.root
        self.cursor = 0
        self.field = "text"
        self.collapsed: set[str] = set()
        self.show_completed = True
        self.stars: set[str] = set()
        self.filter_labels: set[str] = set()
        self._filter_cache = None
        self._visible_cache = None
        self._changing = False
        self.filter_edit_uid: str | None = None
        self.undo_stack: list[Snapshot] = []
        self.redo_stack: list[Snapshot] = []
        self._group = None
        self._group_time = 0.0
        self._saved = self.signature()
        self.revision = 0
        self._dirty_revision = -1
        self._dirty_value = False

    def snapshot(self) -> Snapshot:
        records = tuple(n.record() for n in self.root.walk())
        return Snapshot(records, self.selected.uid, self.zoom.uid, self.cursor,
                        self.field, frozenset(self.collapsed), frozenset(self.filter_labels), self.filter_edit_uid)

    def signature(self) -> tuple:
        return tuple(n.record() for n in self.root.walk())

    @property
    def dirty(self) -> bool:
        if self._dirty_revision != self.revision:
            self._dirty_value = self.signature() != self._saved
            self._dirty_revision = self.revision
        return self._dirty_value

    def mark_saved(self) -> None:
        self._saved = self.signature()
        self._dirty_revision, self._dirty_value = self.revision, False

    def mark_unsaved(self) -> None:
        self._saved = None
        self._dirty_revision = -1

    def restore(self, snapshot: Snapshot) -> None:
        nodes = {}
        for uid, parent, attrs, extras in snapshot.records:
            node = Node(dict(attrs), uid=uid, extras=extras)
            nodes[uid] = node
            if parent is not None:
                nodes[parent].add(node)
        self.root = nodes[snapshot.records[0][0]]
        self.selected = nodes.get(snapshot.selected, self.root)
        self.zoom = nodes.get(snapshot.zoom, self.root)
        self.cursor, self.field = snapshot.cursor, snapshot.field
        self.collapsed = set(snapshot.collapsed)
        self.filter_labels = set(snapshot.filters)
        self.filter_edit_uid = snapshot.filter_edit_uid
        self._visible_cache = self._filter_cache = None
        self.revision += 1

    def change(self, action: Callable[[], object], group: str | None = None) -> bool:
        before = self.snapshot()
        editing = self.selected
        filtered_edit = bool(self.filter_labels) and any(n is editing for n, _ in self.visible())
        now = time.monotonic()
        key = (group, self.selected.uid, self.field)
        self._changing = True
        try:
            action()
        except BaseException:
            self.restore(before)
            raise
        finally:
            self._changing = False
            self._visible_cache = self._filter_cache = None
        if self.snapshot().records == before.records:
            self.break_group()
            return False
        if not (group and key == self._group and now - self._group_time < 0.7):
            self.undo_stack.append(before)
            if len(self.undo_stack) > 200:
                del self.undo_stack[0]
        self._group, self._group_time = key if group else None, now
        self.redo_stack.clear()
        self.revision += 1
        if filtered_edit and self.selected is editing:
            self.filter_edit_uid = editing.uid
        return True

    def break_group(self) -> None:
        self._group = None

    def undo(self) -> bool:
        self.break_group()
        if not self.undo_stack:
            return False
        self.redo_stack.append(self.snapshot())
        self.restore(self.undo_stack.pop())
        return True

    def redo(self) -> bool:
        self.break_group()
        if not self.redo_stack:
            return False
        self.undo_stack.append(self.snapshot())
        self.restore(self.redo_stack.pop())
        return True

    def visible(self) -> list[tuple[Node, int]]:
        cache_key = (id(self.root), self.revision, self.zoom.uid, self.show_completed,
                     frozenset(self.collapsed), frozenset(self.filter_labels), self.filter_edit_uid)
        if not self._changing and self._visible_cache is not None and self._visible_cache[0] == cache_key:
            return self._visible_cache[1]
        result = []
        allowed = None
        if self.filter_labels:
            from .search import labels
            key = (self.revision, self.zoom.uid, frozenset(self.filter_labels))
            if self._changing or self._filter_cache is None or self._filter_cache[0] != key:
                allowed = set()
                for node in self.zoom.walk():
                    if self.filter_labels <= labels(node.text + "\n" + node.note):
                        allowed.add(node.uid)
                        for parent in self.ancestors(node):
                            if parent.uid in allowed:
                                break
                            allowed.add(parent.uid)
                self._filter_cache = key, allowed
            allowed = self._filter_cache[1]
            if self.filter_edit_uid == self.selected.uid:
                allowed = allowed | {self.selected.uid} | {n.uid for n in self.ancestors(self.selected)}
        if self.zoom is not self.root:
            result.append((self.zoom, -1))
        if allowed is None and self.zoom.uid in self.collapsed:
            self._visible_cache = cache_key, result
            return result
        stack = [(n, 0) for n in reversed(self.zoom.children)]
        while stack:
            node, depth = stack.pop()
            if allowed is not None and node.uid not in allowed:
                continue
            if allowed is None and not self.show_completed and node.completed:
                continue
            result.append((node, depth))
            if allowed is not None or node.uid not in self.collapsed:
                stack.extend((n, depth + 1) for n in reversed(node.children))
        self._visible_cache = cache_key, result
        return result

    @property
    def value(self) -> str:
        return richtext.plain(self.selected.attrs.get(self.field, ""))

    def select(self, node: Node, cursor: int = 0, field: str = "text") -> None:
        self.break_group()
        if node.uid != self.filter_edit_uid:
            self.filter_edit_uid = None
        self.selected, self.field = node, field
        self.cursor = max(0, min(cursor, len(self.value)))

    def ensure_selection(self) -> None:
        visible = {n.uid: n for n, _ in self.visible()}
        node = self.selected
        while node.uid not in visible and node.parent:
            node = node.parent
        if node.uid not in visible:
            node = next(iter(visible.values()), self.zoom)
        if node is not self.selected:
            self.select(node)
        self.cursor = max(0, min(self.cursor, len(self.value)))

    def zoom_in(self, node: Node | None = None) -> None:
        node = node if node is not None else self.selected
        if node is self.root:
            return
        self.zoom = node
        self.collapsed.discard(node.uid)
        self.select(node, min(self.cursor, len(node.text)))

    def zoom_out(self) -> None:
        if self.zoom.parent:
            previous = self.zoom
            self.zoom = previous.parent
            self.collapsed.discard(previous.uid)
            self.select(previous)

    def home(self) -> None:
        self.zoom = self.root
        for node in self.ancestors(self.selected):
            self.collapsed.discard(node.uid)
        self.ensure_selection()

    @staticmethod
    def ancestors(node: Node) -> Iterator[Node]:
        while node.parent:
            node = node.parent
            yield node

    def toggle_fold(self, expanded: bool | None = None, recursive: bool = False) -> None:
        node = self.selected
        if not node.children:
            return
        expand = node.uid in self.collapsed if expanded is None else expanded
        for n in node.walk() if recursive else (node,):
            if expand:
                self.collapsed.discard(n.uid)
            elif n.children:
                self.collapsed.add(n.uid)
        self.ensure_selection()
        self.break_group()

    def replace(self, start: int, end: int, text: str) -> None:
        if start == end and not text:
            return
        if self.selected is self.root:
            node = Node()
            self.root.add(node)
            self.select(node)
        source = self.selected.attrs.get(self.field, "")
        value = richtext.replace(source, start, end, text)
        if self.field == "_note" and not value:
            self.selected.attrs.pop(self.field, None)
        else:
            self.selected.attrs[self.field] = value
        self.cursor = start + len(text)

    def insert(self, text: str) -> None:
        self.replace(self.cursor, self.cursor, text)

    def enter(self) -> None:
        node = self.selected
        if self.field == "_note":
            self.insert("\n")
            return
        if node is self.zoom:
            child = Node()
            node.add(child, 0)
            self.collapsed.discard(node.uid)
            self.select(child)
            return
        if not node.text and not node.note and not node.children and node.parent is not self.zoom:
            self.outdent()
            return
        parent = node.parent
        if parent is None:
            return
        index = parent.children.index(node)
        before, after = richtext.split(node.attrs.get("text", ""), self.cursor)
        if self.cursor == 0 and node.text:
            parent.add(Node({"text": before}), index)
            self.select(node)
        else:
            node.attrs["text"] = before
            sibling = Node({"text": after})
            parent.add(sibling, index + 1)
            self.select(sibling)

    def indent(self) -> None:
        node = self.selected
        if node is self.zoom or node.parent is None:
            return
        siblings = node.parent.children
        index = siblings.index(node)
        if index == 0:
            return
        parent = siblings[index - 1]
        siblings.pop(index)
        parent.add(node)
        self.collapsed.discard(parent.uid)

    def outdent(self) -> None:
        node = self.selected
        parent = node.parent
        if node is self.zoom or parent is None or parent is self.zoom or parent.parent is None:
            return
        grandparent = parent.parent
        parent.children.remove(node)
        grandparent.add(node, grandparent.children.index(parent) + 1)

    def move(self, direction: int) -> None:
        node = self.selected
        if node is self.zoom or node.parent is None:
            return
        siblings = node.parent.children
        index = siblings.index(node)
        target = index + direction
        if 0 <= target < len(siblings):
            siblings[index], siblings[target] = siblings[target], siblings[index]

    def delete_node(self) -> None:
        node = self.selected
        if node is self.root:
            return
        if node is self.zoom:
            self.zoom_out()
        visible = [n for n, _ in self.visible()]
        subtree = {n.uid for n in node.walk()}
        index = visible.index(node) if node in visible else 0
        candidates = [n for n in visible[:index] if n.uid not in subtree]
        following = [n for n in visible[index + 1:] if n.uid not in subtree]
        parent = node.parent
        parent.children.remove(node)
        if not self.root.children:
            self.root.add(Node())
        target = candidates[-1] if candidates else (following[0] if following else parent)
        self.select(target, len(target.text))
        self.ensure_selection()

    def backspace(self) -> None:
        if self.cursor:
            self.replace(self.cursor - 1, self.cursor, "")
            return
        if self.field == "_note":
            if not self.value:
                self.selected.attrs.pop("_note", None)
                self.select(self.selected, len(self.selected.text))
            return
        node = self.selected
        if node is self.zoom or node.parent is None:
            return
        siblings = node.parent.children
        index = siblings.index(node)
        if not node.text and not node.note and not node.children:
            if (set(node.attrs) <= {"text", "_note"} and not node.extras
                    and (len(siblings) != 1 or node.parent is not self.root)):
                self.delete_node()
        elif index == 0:
            self.outdent()
        else:
            previous = siblings[index - 1]
            if set(node.attrs) - {"text", "_note"} or node.extras:
                return
            position = len(previous.text)
            previous.attrs["text"] = previous.attrs.get("text", "") + node.attrs.get("text", "")
            if node.note:
                previous.attrs["_note"] = (
                    previous.attrs.get("_note", "") + ("\n" if previous.note else "")
                    + node.attrs["_note"]
                )
            for child in list(node.children):
                previous.add(child)
            siblings.remove(node)
            self.collapsed.discard(previous.uid)
            self.select(previous, position)

    def toggle_complete(self) -> None:
        if self.selected is self.root:
            return
        if self.selected.completed:
            self.selected.attrs.pop("_complete", None)
        else:
            self.selected.attrs["_complete"] = "true"
        self.ensure_selection()

    @staticmethod
    def clone(node: Node) -> Node:
        result = Node(dict(node.attrs), extras=node.extras)
        stack = [(node, result)]
        while stack:
            source, target = stack.pop()
            for child in source.children:
                copy = Node(dict(child.attrs), extras=child.extras)
                target.add(copy)
                stack.append((child, copy))
        return result

    def duplicate(self) -> None:
        node = self.selected
        if node.parent is None:
            return
        duplicate = self.clone(node)
        if node is self.zoom:
            self.zoom_out()
        node.parent.add(duplicate, node.parent.children.index(node) + 1)
        self.select(duplicate)

    def transfer(self, destination: Node, *, copy: bool = False) -> None:
        """Append the selected tree to a destination in this document."""
        node = self.selected
        if node is self.root:
            raise ValueError("Select a bullet to move or copy")
        if not any(candidate is destination for candidate in self.root.walk()):
            raise ValueError("Destination is no longer in this document")
        if not copy and (destination is node or node in self.ancestors(destination)):
            raise ValueError("Cannot move a branch inside itself")
        result = self.clone(node) if copy else node
        destination.add(result)
        self.zoom = self.root
        self.reveal(result)

    def validate(self) -> None:
        seen, identities = set(), set()
        stack = [(self.root, None)]
        while stack:
            node, parent = stack.pop()
            if id(node) in seen or node.uid in identities or node.parent is not parent:
                raise ValueError("Invalid tree: cycle, duplicate identity, or inconsistent parent")
            if "text" not in node.attrs:
                raise ValueError("A bullet is missing its text attribute")
            seen.add(id(node))
            identities.add(node.uid)
            stack.extend((child, node) for child in node.children)

    def insert_trees(self, trees: list[Node], *, parent: Node | None = None,
                     index: int | None = None) -> list[Node]:
        """Copy OPML branches after the selection, replacing only a truly empty leaf."""
        if not trees:
            return []
        current = self.selected
        if parent is None:
            if current is self.root:
                parent, index = self.root, len(self.root.children)
            else:
                parent = current.parent
                index = parent.children.index(current) + 1
                if (not current.text and not current.note and not current.children and not current.extras
                        and set(current.attrs) <= {"text", "_note"}):
                    index -= 1
                    parent.children.remove(current)
                if current is self.zoom:
                    self.zoom = parent
        index = len(parent.children) if index is None else max(0, min(index, len(parent.children)))
        if parent is self.root and len(parent.children) == 1:
            placeholder = parent.children[0]
            if (not placeholder.text and not placeholder.note and not placeholder.children and not placeholder.extras
                    and set(placeholder.attrs) <= {"text", "_note"}):
                parent.children.clear()
                index = 0
        inserted = []
        for offset, tree in enumerate(trees):
            node = self.clone(tree)
            parent.add(node, index + offset)
            inserted.append(node)
        self.filter_labels.clear()
        for ancestor in (parent, *self.ancestors(parent)):
            self.collapsed.discard(ancestor.uid)
        self.select(inserted[0])
        return inserted

    def paste_outline(self, text: str) -> None:
        """Paste indented lines as bullets in a single undo action."""
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if lines and not lines[-1]:
            lines.pop()
        if len(lines) <= 1:
            self.insert(text.rstrip("\r\n"))
            return
        if self.field == "_note":
            self.insert(text)
            return
        current = self.selected
        if current is self.zoom:
            parent, index = current, 0
        elif (not current.text and not current.children and not current.note and not current.extras
              and set(current.attrs) <= {"text", "_note"}):
            parent = current.parent
            index = parent.children.index(current)
            parent.children.remove(current)
        else:
            parent = current.parent
            index = parent.children.index(current) + 1
        levels: list[tuple[int, Node]] = []
        last = None
        for line in lines:
            expanded = line.expandtabs(4)
            depth = len(expanded) - len(expanded.lstrip(" "))
            value = expanded.lstrip(" ")
            if value.startswith(("- ", "* ", "• ")):
                value = value[2:]
            while levels and depth <= levels[-1][0]:
                levels.pop()
            node = Node({"text": richtext.encode(value)})
            if levels:
                levels[-1][1].add(node)
                self.collapsed.discard(levels[-1][1].uid)
            else:
                parent.add(node, index)
                index += 1
            levels.append((depth, node))
            last = node
        if last:
            self.select(last, len(last.text))

    def search(self, query: str) -> list[Node]:
        words = query.casefold().split()
        if not words:
            return []
        return [n for n in self.root.walk() if n is not self.root
                and all(w in (n.text + "\n" + n.note).casefold() for w in words)]

    def reveal(self, node: Node) -> None:
        self.filter_labels.clear()
        self.filter_edit_uid = None
        ancestors = list(self.ancestors(node))
        if self.zoom is not node and self.zoom not in ancestors:
            self.zoom = self.root
        for parent in ancestors:
            self.collapsed.discard(parent.uid)
        if node.completed or any(n.completed for n in ancestors):
            self.show_completed = True
        self.select(node)
