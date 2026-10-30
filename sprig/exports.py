"""Workflowy-style text/Markdown exports, including notes and rich text."""

import html
from pathlib import Path
import re

from . import richtext
from .model import Editor, Node
from .opml import OpmlFile
from .storage import digest, file_lock, read_limited


def markdown(source: str) -> str:
    result = []
    links = []
    markers = {"b": "**", "strong": "**", "i": "*", "em": "*", "s": "~~",
               "strike": "~~", "del": "~~", "code": "`", "u": "<u>"}
    for token in richtext.tokens(source):
        if token.text:
            value = token.text.replace("\ufeff", "")
            if "code" not in token.style:
                value = re.sub(r"([\\`*_\[\]<>~])", r"\\\1", value)
            result.append(value)
            continue
        match = re.match(r"<(\/?)([a-z]+)", token.raw, re.I)
        if not match:
            continue
        closing, tag = bool(match[1]), match[2].lower()
        if tag in markers:
            result.append("</u>" if tag == "u" and closing else markers[tag])
        elif tag == "a":
            if closing:
                href = links.pop() if links else ""
                result.append("](" + href.replace(" ", "%20").replace(")", "%29") + ")")
            else:
                href = re.search(r'''\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))''', token.raw, re.I)
                links.append(html.unescape(next((g for g in href.groups() if g is not None), "")) if href else "")
                result.append("[")
    return "".join(result)


def render(editor: Editor, root: Node, kind: str, title: str = "Outline") -> str:
    if kind not in ("txt", "md"):
        raise ValueError("Export format must be txt or md")

    def text(raw: str) -> str:
        return markdown(raw) if kind == "md" else richtext.plain(raw).replace("\ufeff", "")

    def note_lines(node: Node, indent: str) -> list[str]:
        if not node.attrs.get("_note"):
            return []
        value = text(node.attrs["_note"])
        lines = value.split("\n")
        if kind == "md":
            return [indent + "> " + line for line in lines]
        lines[0] = '"' + lines[0]
        lines[-1] += '"'
        return [indent + line for line in lines]

    if root is editor.root and len(root.children) == 1:
        root = root.children[0]
    synthetic = root is editor.root
    lines = [title if synthetic else text(root.attrs.get("text", ""))]
    if not synthetic and root.completed:
        lines[0] = "~~" + lines[0] + "~~" if kind == "md" else "[x] " + lines[0]
    if not synthetic:
        lines.extend(note_lines(root, ""))
    lines.append("")
    stack = [(n, 0) for n in reversed(root.children)]
    while stack:
        node, depth = stack.pop()
        indent = "  " * depth
        value = text(node.attrs.get("text", ""))
        body = value.split("\n")
        prefix = "[x] " if node.completed else ""
        lines.append(indent + "- " + prefix + body[0])
        lines.extend(indent + "  " + line for line in body[1:])
        lines.extend(note_lines(node, indent + "  "))
        stack.extend((n, depth + 1) for n in reversed(node.children))
    return "\n".join(lines) + "\n"


def save_export(path: Path, content: str, kind: str, *, overwrite: bool = False, expected: str | None = None) -> None:
    path = path.expanduser().resolve()
    if path.suffix.lower() != "." + kind:
        raise ValueError(f"Choose a .{kind} filename")
    with file_lock(path):
        try:
            initial = digest(read_limited(path))
        except FileNotFoundError:
            initial = None
        if initial is not None and not overwrite:
            raise FileExistsError(path)
        if expected is not None and expected != initial:
            raise OSError("Export destination changed since confirmation")
        def verify():
            try:
                latest = digest(read_limited(path))
            except FileNotFoundError:
                latest = None
            if latest != initial:
                raise OSError("Export destination changed while writing")
        OpmlFile._atomic(path, content.encode("utf-8"), before_replace=verify)
