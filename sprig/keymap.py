"""Named editor commands and configurable terminal key bindings."""

from .keys import Event


# command: (internal event, description, default bindings)
COMMANDS = {
    "menu": ("escape", "Open File / Node / Navigate", ("escape",)),
    "help": ("f1", "Keyboard help", ("f1", "ctrl+/")),
    "quit": ("ctrl+q", "Quit", ("ctrl+q",)),
    "save": ("ctrl+s", "Save document", ("ctrl+s", "f2")),
    "save_as": ("f4", "Save as", ("f4",)),
    "open": ("f3", "Open file browser", ("f3",)),
    "close": ("ctrl+w", "Close document", ("ctrl+w",)),
    "new_node": ("enter", "Split / add a bullet", ("enter",)),
    "indent": ("tab", "Indent branch", ("tab", "alt+shift+right")),
    "outdent": ("shift+tab", "Outdent branch", ("shift+tab", "alt+shift+left")),
    "zoom_in": ("f7", "Zoom in", ("alt+.", "f7")),
    "zoom_out": ("f8", "Zoom out", ("alt+,", "f8")),
    "home": ("f12", "Return Home", ("f12",)),
    "back": ("alt+left", "Navigation back", ("alt+left", "alt+b", "ctrl+alt+left")),
    "forward": ("alt+right", "Navigation forward", ("alt+right", "alt+f", "ctrl+alt+right")),
    "collapse": ("ctrl+,", "Collapse current node", ("ctrl+,", "ctrl+up", "ctrl+<", "ctrl+shift+,", "ctrl+shift+<")),
    "expand": ("ctrl+.", "Expand current node", ("ctrl+.", "ctrl+down", "ctrl+>", "ctrl+shift+.", "ctrl+shift+>")),
    "toggle_fold": ("f9", "Toggle current node", ("f9", "ctrl+space")),
    "collapse_all": ("ctrl+alt+up", "Collapse all descendants", ("ctrl+alt+up",)),
    "expand_all": ("ctrl+alt+down", "Expand all descendants", ("ctrl+alt+down",)),
    "note": ("f6", "Edit note / return to bullet", ("f6", "shift+enter")),
    "complete": ("f5", "Complete / uncomplete", ("f5", "ctrl+enter")),
    "show_completed": ("ctrl+o", "Show / hide completed", ("ctrl+o",)),
    "duplicate": ("ctrl+d", "Duplicate subtree", ("ctrl+d", "alt+d", "alt+shift+d")),
    "delete": ("f10", "Confirm deletion to Trash", ("f10", "ctrl+shift+backspace")),
    "move_up": ("alt+up", "Move branch up", ("alt+up", "alt+shift+up", "ctrl+shift+up")),
    "move_down": ("alt+down", "Move branch down", ("alt+down", "alt+shift+down", "ctrl+shift+down")),
    "move_to": ("ctrl+alt+m", "Move to destination picker", ("ctrl+alt+m",)),
    "copy_to": ("ctrl+alt+c", "Copy to destination picker", ("ctrl+alt+c",)),
    "star": ("alt+s", "Star / unstar node", ("alt+s",)),
    "stars": ("f11", "Browse starred nodes", ("f11",)),
    "labels": ("ctrl+t", "Hashtag / mention filters", ("ctrl+t",)),
    "search": ("ctrl+f", "Fuzzy search", ("ctrl+f", "ctrl+k")),
    "undo": ("ctrl+z", "Undo", ("ctrl+z",)),
    "redo": ("ctrl+y", "Redo", ("ctrl+y", "ctrl+shift+z")),
    "copy": ("ctrl+c", "Copy text or branch", ("ctrl+c",)),
    "cut": ("ctrl+x", "Cut text or branch", ("ctrl+x",)),
    "paste": ("ctrl+v", "Paste internal clipboard", ("ctrl+v",)),
    "bold": ("ctrl+b", "Bold", ("ctrl+b",)),
    "italic": ("alt+i", "Italic", ("alt+i",)),
    "underline": ("ctrl+u", "Underline", ("ctrl+u",)),
    "select_all": ("ctrl+a", "Select all text in field", ("ctrl+a",)),
    "delete_word": ("ctrl+backspace", "Delete previous word", ("ctrl+backspace", "alt+backspace")),
    "word_left": ("ctrl+left", "Move left by word", ("ctrl+left",)),
    "word_right": ("ctrl+right", "Move right by word", ("ctrl+right",)),
    "first_node": ("ctrl+home", "First visible node", ("ctrl+home",)),
    "last_node": ("ctrl+end", "Last visible node", ("ctrl+end",)),
}

DEFAULT_BINDINGS = {name: list(value[2]) for name, value in COMMANDS.items()}
SPECIAL_KEYS = {"escape", "enter", "tab", "backspace", "delete", "home", "end",
                "up", "down", "left", "right", "pageup", "pagedown", "space"}


def normalize_key(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Key bindings must be nonempty strings")
    value = value.strip().lower().replace("control+", "ctrl+")
    parts = value[:-2].split("+") + ["+"] if value.endswith("++") else value.split("+")
    modifiers, key = parts[:-1], parts[-1]
    key = {"esc": "escape", "return": "enter", "pgup": "pageup", "pgdn": "pagedown"}.get(key, key)
    if len(set(modifiers)) != len(modifiers) or any(m not in ("ctrl", "alt", "shift") for m in modifiers):
        raise ValueError(f"Invalid key modifiers: {value}")
    function = key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24
    if key not in SPECIAL_KEYS and not function and not (len(key) == 1 and ("ctrl" in modifiers or "alt" in modifiers)):
        raise ValueError(f"Use a special key, function key, or Ctrl/Alt combination: {value}")
    return "+".join([m for m in ("ctrl", "alt", "shift") if m in modifiers] + [key])


class KeyMap:
    def __init__(self, overrides: dict | None = None) -> None:
        overrides = {} if overrides is None else overrides
        if not isinstance(overrides, dict) or any(name not in COMMANDS for name in overrides):
            raise ValueError("Unknown command in [keys]")
        self.bindings = {}
        self.commands = {}
        for name, defaults in DEFAULT_BINDINGS.items():
            values = overrides.get(name, defaults)
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, (list, tuple)):
                raise ValueError(f"keys.{name} must be a string or an array of strings")
            bindings = list(dict.fromkeys(normalize_key(value) for value in values))
            self.bindings[name] = bindings
            for key in bindings:
                if key in self.commands:
                    raise ValueError(f"Key {key} is assigned to both {self.commands[key]} and {name}")
                self.commands[key] = name
        self.original_keys = {key for values in DEFAULT_BINDINGS.values() for key in values}

    def command(self, event: Event) -> str | None:
        return self.commands.get(event.key)

    def translate(self, event: Event) -> Event:
        command = self.command(event)
        if command:
            return Event(COMMANDS[command][0], event.text)
        return Event("unbound") if event.key in self.original_keys else event

    def label(self, command: str, *, alternatives: bool = False) -> str:
        values = self.bindings[command]
        if not values:
            return "Unbound"
        def pretty(key):
            names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "escape": "Esc"}
            return "+".join(names.get(part, part.upper() if (len(part) == 1 and part.isalpha()) or (part.startswith("f") and part[1:].isdigit())
                                      else part.capitalize() if len(part) > 1 else part) for part in key.split("+"))
        return " / ".join(pretty(value) for value in (values[:2] if alternatives else values[:1]))

    def footer(self) -> str:
        def short(name):
            label = self.label(name)
            return "^" + label[-1] if label.startswith("Ctrl+") and len(label) == 6 and label[-1].isalpha() else label
        return (f"{short('menu')} menu  {short('toggle_fold')} fold  {short('zoom_in')}/{short('zoom_out')} zoom  "
                f"{short('search')} find  {short('save')} save  {short('quit')} quit")

    def help_lines(self) -> list[str]:
        return ["Type directly to edit. There is no insert mode.",
                "Bindings: ~/.config/sprig-tui/config.toml → [keys]",
                "File → Reload config applies edits. Esc closes help.", ""] + [
                    f"{self.label(name, alternatives=True):24} {description}"
                    for name, (_, description, _) in COMMANDS.items()
                ] + ["", "Dialog arrows, Enter, Esc, and text editing use their usual keys.",
                     "Some terminals reserve key combinations; F9 toggles folding by default.",
                     "Click triangles to fold; bullets to zoom; breadcrumbs to go back.",
                     "Use Up/Down/Page Up/Page Down to scroll this help."]
