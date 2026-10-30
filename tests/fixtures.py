"""Fictional fixtures: public tests never depend on someone's personal notes."""

from pathlib import Path

from sprig.model import Editor, Node
from sprig.opml import OpmlFile


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def full_bytes() -> bytes:
    root = Node(uid="root")
    nodes = []
    parent = root
    for i in range(15):
        node = Node({"text": f"Observatory level {i + 1}: 日本語 🌱 café"})
        parent.add(node)
        nodes.append(node)
        parent = node
    groups = []
    for i in range(80):
        group = Node({"text": f"Sky survey {i + 1} #space @Sky"})
        root.add(group)
        groups.append(group)
        nodes.append(group)
    for i in range(8852 - len(nodes)):
        node = Node({"text": f"Observation {i + 1}: <b>review</b> &amp; record #space @Sky"})
        groups[i % len(groups)].add(node)
        nodes.append(node)
    for i, node in enumerate(nodes):
        if i < 915:
            node.attrs["_note"] = "Clear skies\n<a href=\"https://example.com/sky\">Sky chart</a> <i>check</i>"
        if i < 32:
            node.attrs["_complete"] = "true"
    nodes[95].attrs["text"] += ' <time datetime="2026-10-30">Oct 30, 2026</time>'
    nodes[96].attrs["_note"] = "Long note 日本語 🌱 " * 700
    nodes[97].attrs["text"] += " extended text" * 190
    return OpmlFile("synthetic-full.opml").encode(Editor(root))
