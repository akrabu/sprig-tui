"""Edit visible text without flattening Workflowy's HTML-valued attributes."""

from dataclasses import dataclass
from functools import lru_cache
import html
import re


_PARTS = re.compile(
    r"</?(?:b|strong|i|em|u|s|strike|del|code|a|span|mark|font|time|mention|br|p|div)\b[^>]*>"
    r"|&(?:#[0-9]+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]+);",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Token:
    raw: str
    text: str
    style: frozenset[str] = frozenset()


@lru_cache(maxsize=128)
def tokens(source: str) -> tuple[Token, ...]:
    result = []
    active: list[str] = []

    def literal(value: str) -> None:
        if value:
            result.append(Token(value, value, frozenset(active)))

    end = 0
    for match in _PARTS.finditer(source):
        literal(source[end:match.start()])
        raw = match.group()
        if raw.startswith("&"):
            result.append(Token(raw, html.unescape(raw), frozenset(active)))
        else:
            tag = re.match(r"</?([a-z]+)", raw, re.IGNORECASE).group(1).lower()
            style = {"strong": "b", "em": "i", "strike": "s", "del": "s"}.get(tag, tag)
            if raw.startswith("</"):
                if style in active:
                    active.remove(style)
            elif style != "br":
                active.append(style)
            result.append(Token(raw, "\n" if tag == "br" else "", frozenset(active)))
        end = match.end()
    literal(source[end:])
    return tuple(result)


@lru_cache(maxsize=32768)
def plain(source: str) -> str:
    if "<" not in source and "&" not in source:
        return source
    # Search and layout need strings, not one Token object per character. Keep
    # rich tokenization bounded to the few fields actually being drawn/edited.
    return _PARTS.sub(lambda m: html.unescape(m.group()) if m.group().startswith("&")
                      else "\n" if re.match(r"<br\b", m.group(), re.I) else "", source)


def encode(text: str) -> str:
    return html.escape(text, quote=False)


def replace(source: str, start: int, end: int, text: str) -> str:
    """Replace visible character offsets while retaining surrounding HTML tags."""
    length = len(plain(source))
    start, end = max(0, min(start, length)), max(0, min(end, length))
    if end < start:
        start, end = end, start
    if start == end and not text:
        return source
    output = []
    position = 0
    inserted = False
    for token in tokens(source):
        if not token.text:
            output.append(token.raw)
            continue
        next_position = position + len(token.text)
        if not inserted and start < next_position:
            prefix = token.text[:max(0, start - position)]
            output.append(encode(prefix))
            output.append(encode(text))
            inserted = True
            output.append(encode(token.text[max(end - position, len(prefix)):]))
        elif next_position <= start or position >= end:
            output.append(token.raw)
        elif next_position > end:
            output.append(encode(token.text[end - position:]))
        position = next_position
    if not inserted:
        output.append(encode(text))
    return "".join(output)


def split(source: str, position: int) -> tuple[str, str]:
    return replace(source, position, len(plain(source)), ""), replace(source, 0, position, "")
