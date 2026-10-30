"""Normalize ncurses, xterm and Kitty keyboard events with portable fallbacks."""

from dataclasses import dataclass
import curses
import re
import time


@dataclass(frozen=True)
class Event:
    key: str
    text: str = ""


_CONTROLS = {chr(i): "ctrl+" + chr(96 + i) for i in range(1, 27)}
_CONTROLS.update({"\x00": "ctrl+space", "\x09": "tab", "\x0a": "enter",
                  "\x0d": "enter", "\x08": "backspace", "\x7f": "backspace",
                  "\x1b": "escape", "\x1f": "ctrl+/"})
_ARROWS = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end"}
_TILDE = {1: "home", 2: "insert", 3: "delete", 4: "end", 5: "pageup", 6: "pagedown",
          7: "home", 8: "end", 11: "f1", 12: "f2", 13: "f3", 14: "f4", 15: "f5",
          17: "f6", 18: "f7", 19: "f8", 20: "f9", 21: "f10", 23: "f11", 24: "f12"}


def modified(key: str, modifier: int) -> str:
    bits = modifier - 1
    return ("ctrl+" if bits & 4 else "") + ("alt+" if bits & 2 else "") + ("shift+" if bits & 1 else "") + key


def decode_sequence(sequence: str) -> Event:
    if sequence == "[Z":
        return Event("shift+tab")
    if len(sequence) == 2 and sequence[0] == "O":
        return Event({"P": "f1", "Q": "f2", "R": "f3", "S": "f4", **_ARROWS}.get(sequence[1], "unknown"))
    arrow = re.fullmatch(r"\[(?:1;([0-9]+))?([ABCDHF])", sequence)
    if arrow:
        return Event(modified(_ARROWS[arrow[2]], int(arrow[1] or 1)))
    tilde = re.fullmatch(r"\[([0-9]+)(?:;([0-9]+))?~", sequence)
    if tilde:
        return Event(modified(_TILDE.get(int(tilde[1]), "unknown"), int(tilde[2] or 1)))
    # Kitty CSI-u and xterm modifyOtherKeys make Ctrl/Shift+Enter unambiguous.
    kitty = re.fullmatch(r"\[([0-9]+)(?::[0-9:]+)?(?:;([0-9]+)(?::[0-9]+)?)?u", sequence)
    xterm = re.fullmatch(r"\[27;([0-9]+);([0-9]+)~", sequence)
    if kitty or xterm:
        code = int(kitty[1] if kitty else xterm[2])
        modifier = int((kitty[2] or 1) if kitty else xterm[1])
        if not 0 <= code <= 0x10ffff or 0xd800 <= code <= 0xdfff or not 1 <= modifier <= 8:
            return Event("unknown")
        special = {13: "enter", 9: "tab", 27: "escape", 127: "backspace", 32: "space",
                   57352: "up", 57353: "down", 57350: "left", 57351: "right",
                   57356: "home", 57357: "end", 57354: "pageup", 57355: "pagedown",
                   57349: "delete", 57348: "insert"}
        if (code == 32 or code not in special) and 32 <= code <= 0x10ffff and modifier in (1, 2):
            char = chr(code)
            if modifier == 2:
                char = char.upper()
            return Event("text", char)
        return Event(modified(special.get(code, chr(code).lower()), modifier))
    return Event("unknown")


class Keyboard:
    def __init__(self, screen) -> None:
        self.screen = screen
        self.screen.timeout(100)

    def normalize(self, key) -> Event:
        if isinstance(key, str):
            return Event(_CONTROLS[key]) if key in _CONTROLS else Event("text", key)
        special = {curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_LEFT: "left",
                   curses.KEY_RIGHT: "right", curses.KEY_HOME: "home", curses.KEY_END: "end",
                   curses.KEY_BACKSPACE: "backspace", curses.KEY_DC: "delete",
                   curses.KEY_PPAGE: "pageup", curses.KEY_NPAGE: "pagedown",
                   curses.KEY_BTAB: "shift+tab", curses.KEY_ENTER: "enter",
                   curses.KEY_RESIZE: "resize", curses.KEY_MOUSE: "mouse",
                   curses.KEY_SLEFT: "shift+left", curses.KEY_SRIGHT: "shift+right",
                   curses.KEY_SHOME: "shift+home", curses.KEY_SEND: "shift+end"}
        if key in special:
            return Event(special[key])
        if curses.KEY_F1 <= key <= curses.KEY_F12:
            return Event("f" + str(key - curses.KEY_F1 + 1))
        name = curses.keyname(key).decode("ascii", "replace")
        extended = re.fullmatch(r"k(UP|DN|LFT|RIT|HOM|END|DC)([2-8])?", name)
        if extended:
            label = {"UP": "up", "DN": "down", "LFT": "left", "RIT": "right",
                     "HOM": "home", "END": "end", "DC": "delete"}[extended[1]]
            return Event(modified(label, int(extended[2] or 2)))
        return Event("unknown")

    def read(self) -> Event | None:
        try:
            key = self.screen.get_wch()
        except curses.error:
            return None
        if key != "\x1b":
            return self.normalize(key)
        self.screen.timeout(35)
        try:
            following = self.screen.get_wch()
            if isinstance(following, int):
                return Event("alt+" + self.normalize(following).key)
            if following not in ("[", "O"):
                event = self.normalize(following)
                return Event("alt+" + (event.text.lower() if event.key == "text" else event.key))
            sequence = following
            for _ in range(48):
                char = self.screen.get_wch()
                if not isinstance(char, str):
                    return Event("unknown")
                sequence += char
                if "@" <= char <= "~":
                    if sequence == "[200~":
                        return self.paste()
                    return decode_sequence(sequence)
            return Event("unknown")
        except curses.error:
            return Event("escape")
        finally:
            self.screen.timeout(100)

    def paste(self) -> Event:
        self.screen.timeout(100)
        result = []
        suffix = ""
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                char = self.screen.get_wch()
            except curses.error:
                continue
            if not isinstance(char, str):
                continue
            deadline = time.monotonic() + 3
            result.append(char)
            suffix = (suffix + char)[-6:]
            if suffix == "\x1b[201~":
                return Event("paste", "".join(result[:-6]))
        return Event("paste", "".join(result))
