"""Preserve OPML metadata, support deep trees, and save files atomically."""

import hashlib
from pathlib import Path
import stat
from typing import Callable
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

from .model import Editor, Node
from .backups import Backups
from .storage import (MAX_DOCUMENT_BYTES, CommittedWriteError, atomic_write,
                      digest, file_lock, read_limited)


class OpmlError(ValueError):
    pass


class FileChangedError(OSError):
    pass


def local_name(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def qualified(tag: str, label: str) -> str:
    return tag.rsplit("}", 1)[0] + "}" + label if tag.startswith("{") else label


def check_container(element: ET.Element) -> None:
    if (element.text or "").strip() or any((child.tail or "").strip() for child in element):
        raise OpmlError("Mixed XML text in an OPML container is unsupported; the file was not changed")


def xml_bytes(root: ET.Element) -> bytes:
    """Serialize without Python's recursion limit, including namespaced metadata."""
    namespaces = {}

    def name(value: str) -> str:
        if value.startswith("{"):
            uri, label = value[1:].split("}", 1)
            if uri == "http://www.w3.org/XML/1998/namespace":
                return "xml:" + label
            if uri not in namespaces:
                namespaces[uri] = "ns" + str(len(namespaces))
            return namespaces[uri] + ":" + label
        return value

    for element in root.iter():
        if isinstance(element.tag, str):
            name(element.tag)
        for attr in element.attrib:
            name(attr)
    output = ['<?xml version="1.0" encoding="UTF-8"?>\n']
    stack = [("element", root)]
    while stack:
        kind, item = stack.pop()
        if kind == "literal":
            output.append(item)
            continue
        element = item
        if element.tag is ET.Comment:
            output.append("<!--" + (element.text or "") + "-->")
        elif element.tag is ET.ProcessingInstruction:
            output.append("<?" + (element.text or "") + "?>")
        else:
            attrs = "".join(" " + name(k) + "=" + quoteattr(v) for k, v in element.attrib.items())
            if element is root:
                attrs += "".join(" xmlns:" + p + "=" + quoteattr(uri)
                                 for uri, p in namespaces.items())
            tag = name(element.tag)
            if len(element) or element.text:
                output.append("<" + tag + attrs + ">")
                if element.text:
                    output.append(escape(element.text))
                stack.append(("literal", "</" + tag + ">"))
                for child in reversed(element):
                    if child.tail:
                        stack.append(("literal", escape(child.tail)))
                    stack.append(("element", child))
            else:
                output.append("<" + tag + attrs + " />")
    output.append("\n")
    return "".join(output).encode("utf-8")


class OpmlFile:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.template = ET.Element("opml", {"version": "2.0"})
        head = ET.SubElement(self.template, "head")
        ET.SubElement(head, "title").text = self.path.stem
        ET.SubElement(self.template, "body")
        self.fingerprint: str | None = None
        self.backup_directory: str | Path | None = None
        self.backup_count = 20
        self.backups_enabled = True
        self.warning = ""

    @property
    def title(self) -> str:
        head_tag = qualified(self.template.tag, "head")
        head = next((e for e in self.template if e.tag == head_tag), None)
        if head is not None:
            title = next((e for e in head if e.tag == qualified(head_tag, "title")), None)
            if title is not None and title.text and title.text.strip():
                return title.text.strip()
        return self.path.stem

    @classmethod
    def open(cls, path: str | Path) -> tuple["OpmlFile", Editor]:
        source = cls(path)
        try:
            content = read_limited(source.path)
        except FileNotFoundError:
            root = Node(uid="root")
            root.add(Node())
            return source, Editor(root)
        return cls.decode(source.path, content)

    @classmethod
    def decode(cls, path: str | Path, content: bytes) -> tuple["OpmlFile", Editor]:
        source = cls(path)
        if len(content) > MAX_DOCUMENT_BYTES:
            raise OpmlError("OPML exceeds 64 MiB")
        # OPML has no need for DTDs; disallow entities and external declarations.
        if b"<!DOCTYPE" in content.upper().replace(b"\x00", b""):
            raise OpmlError("DTD declarations are not supported in OPML")
        try:
            parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
            template = ET.fromstring(content, parser=parser)
        except ET.ParseError as error:
            raise OpmlError(f"Invalid OPML: {error}") from error
        if local_name(template.tag) != "opml":
            raise OpmlError("Expected an <opml> document.")
        body_tag = qualified(template.tag, "body")
        outline_tag = qualified(template.tag, "outline")
        bodies = [e for e in template if e.tag == body_tag]
        if len(bodies) != 1:
            raise OpmlError("OPML must contain exactly one <body>.")
        check_container(template)
        check_container(bodies[0])
        source.template = template
        source.fingerprint = hashlib.sha256(content).hexdigest()
        root = Node(uid="root")
        stack = [(e, root) for e in reversed(bodies[0]) if e.tag == outline_tag]
        while stack:
            element, parent = stack.pop()
            check_container(element)
            if "text" not in element.attrib:
                raise OpmlError("An <outline> is missing its required 'text' attribute.")
            node = Node(dict(element.attrib), extras=tuple(
                (i, e) for i, e in enumerate(element) if e.tag != outline_tag
            ))
            parent.add(node)
            stack.extend((e, node) for e in reversed(element) if e.tag == outline_tag)
        return source, Editor(root)

    def encode(self, editor: Editor) -> bytes:
        editor.validate()
        root = ET.Element(self.template.tag, dict(self.template.attrib))
        root.text = "\n  "
        body_tag = qualified(self.template.tag, "body")
        body_template = next((e for e in self.template if e.tag == body_tag), None)
        body = ET.Element(body_tag, dict(body_template.attrib) if body_template is not None else {})
        body.text = "\n    " if editor.root.children else None
        outline_tag = qualified(body_tag, "outline")
        pairs = []
        stack = [(n, body, 2) for n in reversed(editor.root.children)]
        while stack:
            node, parent, depth = stack.pop()
            element = ET.SubElement(parent, outline_tag, node.attrs)
            indent = "  " * min(depth, 32)
            element.tail = "\n" + indent
            if node.children or node.extras:
                element.text = "\n" + "  " * min(depth + 1, 32)
            pairs.append((node, element))
            stack.extend((n, element, depth + 1) for n in reversed(node.children))
        for node, element in pairs:
            for position, extra in node.extras:
                element.insert(min(position, len(element)), extra)
        for element in self.template:
            root.append(body if element.tag == body_tag else element)
        head_tag = qualified(self.template.tag, "head")
        if not any(e.tag == head_tag for e in self.template):
            root.insert(0, ET.Element(head_tag))
        if body_template is None:
            root.append(body)
        else:
            for position, extra in enumerate(body_template):
                if extra.tag != outline_tag:
                    body.insert(min(position, len(body)), extra)
        content = xml_bytes(root)
        if len(content) > MAX_DOCUMENT_BYTES:
            raise OpmlError("Encoded OPML exceeds 64 MiB")
        try:
            ET.fromstring(content)
        except ET.ParseError as error:
            raise OpmlError(f"Cannot save invalid XML: {error}") from error
        return content

    @staticmethod
    def _atomic(path: Path, content: bytes, mode: int = 0o600,
                before_replace: Callable[[], None] | None = None) -> None:
        atomic_write(path, content, mode, before_replace)

    def save(self, editor: Editor, path: str | Path | None = None, *, overwrite: bool = False,
             expected: str | None = None) -> None:
        target = Path(path).expanduser().resolve() if path is not None else self.path
        with file_lock(target):
            self._save_locked(editor, target, overwrite=overwrite, expected=expected)

    def _save_locked(self, editor: Editor, target: Path, *, overwrite: bool,
                     expected: str | None) -> None:
        self.warning = ""
        same = target == self.path
        try:
            previous = read_limited(target)
            mode = stat.S_IMODE(target.stat().st_mode)
        except FileNotFoundError:
            previous, mode = None, 0o600
        current = hashlib.sha256(previous).hexdigest() if previous is not None else None
        if same and current != self.fingerprint:
            raise FileChangedError("File changed on disk. Use F4 (Save as) to keep your edits in another file.")
        if not same and previous is not None and not overwrite:
            raise FileExistsError(f"{target} already exists")
        if not same and expected is not None and current != expected:
            raise FileChangedError("Destination changed since confirmation; choose Save as again")

        def verify_unchanged() -> None:
            try:
                latest = digest(read_limited(target))
            except FileNotFoundError:
                latest = None
            if latest != current:
                raise FileChangedError("File changed while saving. Use F4 (Save as) to keep your edits.")

        editor.validate()
        backups = Backups(target, self.backup_count, self.backup_directory)
        if same and previous is not None and editor.signature() == editor._saved:
            if self.backups_enabled:
                backups.create(previous)
                backups.prune()
            return
        content = self.encode(editor)
        if content != previous:
            if previous is not None:
                if self.backups_enabled:
                    backups.create(previous)
                self._atomic(target.with_name(target.name + ".bak"), previous, mode)
            try:
                self._atomic(target, content, mode, before_replace=verify_unchanged)
            except CommittedWriteError as error:
                self.warning = str(error)
        self.path = target
        self.fingerprint = hashlib.sha256(content).hexdigest()
        editor.mark_saved()
        if self.backups_enabled:
            try:
                backups.create(content)
                backups.prune()
            except (OSError, ValueError) as error:
                self.warning = self.warning or f"Saved, but older backups could not be rotated: {error}"
