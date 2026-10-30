"""Labels and ranked subsequence search, without an external fzf dependency."""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re

from .model import Node


LABEL = re.compile(r"(?<![\w/#@])([#@]\w+(?:[-/]\w+)*)", re.UNICODE)


@lru_cache(maxsize=16384)
def label_spans(text: str) -> tuple[tuple[int, int, str], ...]:
    return tuple((m.start(1), m.end(1), m.group(1)) for m in LABEL.finditer(text))


def labels(text: str) -> set[str]:
    return {value.casefold() for _, _, value in label_spans(text)}


def fuzzy(query: str, text: str) -> tuple[int, tuple[int, ...]] | None:
    """Rank contiguous and word-initial matches above scattered subsequences."""
    needle, haystack = query.casefold(), text.casefold()
    def offsets(positions):
        if len(haystack) == len(text):
            return tuple(positions)
        mapping = [index for index, char in enumerate(text) for _ in char.casefold()]
        return tuple(dict.fromkeys(mapping[index] for index in positions))
    if not needle:
        return 0, ()
    contiguous = haystack.find(needle)
    if contiguous >= 0:
        boundary = contiguous == 0 or not haystack[contiguous - 1].isalnum()
        return contiguous // 8 - (40 if boundary else 20), offsets(range(contiguous, contiguous + len(needle)))
    positions = []
    cursor = 0
    score = 0
    previous = -2
    for char in needle:
        index = haystack.find(char, cursor)
        if index < 0:
            return None
        score += max(0, index - previous - 1)
        if index == 0 or not haystack[index - 1].isalnum():
            score -= 8
        if index == previous + 1:
            score -= 4
        positions.append(index)
        previous, cursor = index, index + 1
    return score, offsets(positions)


def rank(query: str, title: str, note: str = "") -> tuple[int, str, tuple[int, ...]] | None:
    total = 0
    title_positions = set()
    note_positions = set()
    for word in query.split():
        # Label queries match labels exactly; ordinary words are fuzzy.
        if word.startswith(("#", "@")):
            matching = [(field, start, end) for field, value in (("text", title), ("_note", note))
                        for start, end, tag in label_spans(value) if tag.casefold() == word.casefold()]
            if not matching:
                return None
            field, start, end = matching[0]
            (title_positions if field == "text" else note_positions).update(range(start, end))
            total -= 40
            continue
        in_title, in_note = fuzzy(word, title), fuzzy(word, note)
        if in_title is None and in_note is None:
            return None
        if in_title is not None and (in_note is None or in_title[0] <= in_note[0] + 60):
            total += in_title[0]
            title_positions.update(in_title[1])
        else:
            total += in_note[0] + 60
            note_positions.update(in_note[1])
    field = "text" if title_positions or not note_positions else "_note"
    return total, field, tuple(sorted(title_positions if field == "text" else note_positions))


@dataclass
class Hit:
    node: Node
    path: Path
    breadcrumb: str
    score: int
    field: str
    positions: tuple[int, ...]


class Index:
    def __init__(self, root: Node, path: Path) -> None:
        self.path = path
        self.entries = []
        self._previous_query = ""
        self._previous_allowed = None
        self._previous_matches = None
        stack = [(n, "") for n in reversed(root.children)]
        while stack:
            node, parents = stack.pop()
            self.entries.append((node, node.text, node.note, parents))
            trail = (parents + " › " if parents else "") + (node.text.replace("\n", " ")[:60] or "(empty)")
            if len(trail) > 220:
                trail = "… › " + trail[-200:]
            stack.extend((n, trail) for n in reversed(node.children))

    def search(self, query: str, allowed: set[str] | None = None) -> list[Hit]:
        result = []
        allowed_key = frozenset(allowed) if allowed is not None else None
        candidates = self.entries
        folded = query.casefold()
        # Extending an ordinary fuzzy query can only remove previous matches.
        # Label queries use exact labels and therefore cannot use this shortcut.
        if (self._previous_matches is not None and self._previous_allowed == allowed_key
                and folded.startswith(self._previous_query) and self._previous_query
                and not any(word.startswith(("#", "@")) for word in query.split())):
            candidates = self._previous_matches
        matches = []
        for entry in candidates:
            node, title, note, parents = entry
            if allowed is not None and node.uid not in allowed:
                continue
            match = rank(query, title, note)
            if match is not None:
                score, field, positions = match
                result.append(Hit(node, self.path, parents, score, field, positions))
                matches.append(entry)
        result.sort(key=lambda h: h.score)
        self._previous_query, self._previous_allowed, self._previous_matches = folded, allowed_key, matches
        return result
