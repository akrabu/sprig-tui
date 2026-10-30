"""Append-only OPML deletion records; restoration copies rather than erases."""

from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from .model import Editor, Node
from .opml import OpmlFile
from .state import materialize, references, resolve


class Trash:
    def __init__(self, source_path: Path) -> None:
        self.path = source_path.parent / "trash.opml"

    def read(self) -> tuple[OpmlFile, Editor]:
        source, editor = OpmlFile.open(self.path)
        if source.fingerprint is None:
            editor.root.children.clear()
            editor.select(editor.root)
            editor.mark_saved()
        return source, editor

    def append(self, source_path: Path, editor: Editor, node: Node) -> str:
        if source_path.resolve() == self.path.resolve():
            raise ValueError("Open Trash from Navigate to restore records; trash cannot delete into itself.")
        source, trash = self.read()
        refs = references(editor, include_paths=False)
        ref = materialize(node.parent.uid, refs)
        deleted = datetime.now(timezone.utc).isoformat(timespec="seconds")
        record_id = uuid4().hex
        starred = []
        stack = [(node, [])]
        while stack:
            child, location = stack.pop()
            if child.uid in editor.stars:
                starred.append(location)
            stack.extend((n, location + [i]) for i, n in enumerate(child.children))
        record = Node({"text": node.text or "(empty bullet)", "_trash_id": record_id,
                       "_trash_source": str(source_path.resolve()), "_trash_parent": json.dumps(ref),
                       "_trash_index": str(node.parent.children.index(node)), "_trash_deleted": deleted,
                       "_trash_starred": "true" if node.uid in editor.stars else "false",
                       "_trash_stars": json.dumps(starred)})
        record.add(Editor.clone(node))
        trash.root.add(record)
        source.save(trash)
        return record_id

    def records(self) -> list[Node]:
        if not self.path.exists():
            return []
        _, editor = self.read()
        return [n for n in reversed(editor.root.children) if n.attrs.get("_trash_id") and n.children]

    @staticmethod
    def original_parent(editor: Editor, record: Node) -> tuple[Node, int, bool]:
        try:
            ref = json.loads(record.attrs["_trash_parent"])
            parent = resolve(editor, ref)
            if parent is not None:
                return parent, int(record.attrs.get("_trash_index", "0")), True
        except (ValueError, KeyError, TypeError):
            pass
        return editor.root, len(editor.root.children), False

    def mark_restored(self, record_id: str, target: Path) -> None:
        source, editor = self.read()
        record = next((n for n in editor.root.children if n.attrs.get("_trash_id") == record_id), None)
        if record is not None:
            record.attrs["_trash_last_restored"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            record.attrs["_trash_restored_to"] = str(target)
            source.save(editor)
