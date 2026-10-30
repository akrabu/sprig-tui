"""Navigation remembers locations, never old document contents."""

from dataclasses import dataclass
import hashlib
from pathlib import Path

from .state import node_hash, resolve


def reference(node) -> dict:
    ancestors = list(reversed([node, *iter_ancestors(node)]))
    context, path = "", []
    for current in ancestors:
        context = hashlib.sha256((context + node_hash(current)).encode()).hexdigest()
        if current.parent:
            path.append(current.parent.children.index(current))
    return {"uid": node.uid, "hash": node_hash(node), "context": context,
            "path": path, "label": node.text[:100]}


def iter_ancestors(node):
    while node.parent:
        node = node.parent
        yield node


@dataclass(frozen=True)
class Location:
    path: Path
    selected: dict
    zoom: dict
    cursor: int
    field: str
    filters: frozenset[str]
    show_completed: bool

    @classmethod
    def capture(cls, app):
        if app.closed:
            return None
        e = app.editor
        return cls(app.source.path, reference(e.selected), reference(e.zoom), e.cursor,
                   e.field, frozenset(e.filter_labels), e.show_completed)

    @property
    def key(self):
        return self.path, self.selected["uid"], self.zoom["uid"], self.filters

    def apply(self, editor) -> None:
        nodes = {n.uid: n for n in editor.root.walk()}
        def find(ref):
            return nodes.get(ref["uid"]) or resolve(editor, ref)
        editor.zoom = find(self.zoom) or editor.root
        selected = find(self.selected) or editor.zoom
        editor.filter_labels = set(self.filters)
        editor.show_completed = self.show_completed
        editor.filter_edit_uid = None
        for parent in [selected, *iter_ancestors(selected)]:
            editor.collapsed.discard(parent.uid)
        editor.select(selected, self.cursor, self.field)
        editor.ensure_selection()


class History:
    def __init__(self):
        self.back: list[Location] = []
        self.forward: list[Location] = []
        self.replaying = False

    def record(self, before, after):
        if not self.replaying and before and after and before.key != after.key:
            if not self.back or self.back[-1].key != before.key:
                self.back.append(before)
                del self.back[:-200]
            self.forward.clear()
