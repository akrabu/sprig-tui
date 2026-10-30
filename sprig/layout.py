"""Terminal cell widths and wrapping, keeping logical character offsets."""

from dataclasses import dataclass
from functools import lru_cache
import unicodedata


def cell_width(char: str) -> int:
    if char == "\t":
        return 4
    if unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def width(text: str) -> int:
    return sum(cell_width(c) for c in text)


def printable(char: str) -> str:
    if char == "\t":
        return "    "
    if unicodedata.category(char) in ("Cc", "Cs"):
        return "?"
    return char


def clip(text: str, cells: int) -> str:
    output = []
    used = 0
    for char in text:
        size = cell_width(char)
        if used + size > cells:
            break
        output.append(printable(char))
        used += size
    return "".join(output)


@dataclass(frozen=True)
class Segment:
    start: int
    end: int


@lru_cache(maxsize=32768)
def wrap(text: str, cells: int) -> tuple[Segment, ...]:
    cells = max(4, cells)
    result = []
    start = 0
    while start < len(text):
        end, used, space = start, 0, -1
        while end < len(text) and text[end] != "\n":
            size = cell_width(text[end])
            if used + size > cells:
                break
            used += size
            if text[end].isspace():
                space = end
            end += 1
        if end < len(text) and text[end] != "\n" and space >= start:
            end = space + 1
        result.append(Segment(start, end))
        start = end + 1 if end < len(text) and text[end] == "\n" else end
    if not result or text.endswith("\n") or width(text[result[-1].start:]) >= cells:
        result.append(Segment(len(text), len(text)))
    return tuple(result)


def offset_at(text: str, start: int, end: int, cells: int) -> int:
    used = 0
    for index in range(start, end):
        size = cell_width(text[index])
        if used + size > cells:
            return index
        used += size
    return end
