"""Validated TOML preferences and conservative edits that retain user comments."""

from copy import deepcopy
import json
from pathlib import Path
import re
import tomllib

from .keymap import DEFAULT_BINDINGS, KeyMap


FIELDS = {
    "autosave_enabled": ("autosave", "enabled"),
    "autosave_interval": ("autosave", "interval_seconds"),
    "autosave_count": ("autosave", "keep"),
    "autosave_directory": ("autosave", "directory"),
    "backups_enabled": ("backup", "enabled"),
    "backup_count": ("backup", "keep"),
    "backup_directory": ("backup", "directory"),
    "save_on_exit": ("files", "save_on_exit"),
    "last_directory": ("files", "last_directory"),
    "search_scope": ("search", "scope"),
    "debug_enabled": ("debug", "enabled"),
    "debug_path": ("debug", "path"),
}


def defaults(directory: Path) -> dict:
    return {"autosave_enabled": True, "autosave_interval": 60.0, "autosave_count": 20,
            "autosave_directory": str(directory / "autosave"), "backups_enabled": True,
            "backup_count": 20, "backup_directory": str(directory / "backup"),
            "save_on_exit": True, "search_scope": "Whole file", "keymap": {},
            "debug_enabled": False, "debug_path": str(directory / "debug.log")}


def decode(content: bytes, directory: Path) -> dict:
    data = tomllib.loads(content.decode("utf-8"))
    if type(data.get("version", 1)) is not int or data.get("version", 1) != 1:
        raise ValueError("Unsupported config.toml version")
    sections = {section for section, _ in FIELDS.values()}
    if set(data) - sections - {"keys", "version"}:
        raise ValueError("Unknown section in config.toml")
    result = defaults(directory)
    for section in sections:
        values = data.get(section, {})
        if not isinstance(values, dict) or set(values) - {key for table, key in FIELDS.values() if table == section}:
            raise ValueError(f"Unknown or invalid setting in [{section}]")
    for name, (section, key) in FIELDS.items():
        if key in data.get(section, {}):
            result[name] = data[section][key]
    result["keymap"] = data.get("keys", {})
    KeyMap(result["keymap"])
    return result


def literal(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def encode(settings: dict) -> bytes:
    lines = ["# Sprig settings. File → Reload config applies changes.",
             "# Relative backup/autosave paths are resolved beside the open OPML.",
             "# Key names use ctrl+, alt+, shift+, arrows, and function keys.",
             "version = 1", ""]
    for section in dict.fromkeys(section for section, _ in FIELDS.values()):
        lines.append("[" + section + "]")
        for name, (table, key) in FIELDS.items():
            if table == section and name in settings:
                lines.append(key + " = " + literal(settings[name]))
        lines.append("")
    lines += ["# Replace a command's bindings, or use [] to disable it.",
              "# Dialog editing/navigation keys stay available.", "[keys]"]
    for name, values in DEFAULT_BINDINGS.items():
        lines.append(name + " = " + literal(settings.get("keymap", {}).get(name, values)))
    return ("\n".join(lines) + "\n").encode()


def _edit(source: str, section: str, key: str, value) -> str:
    original = tomllib.loads(source)
    expected = deepcopy(original)
    expected.setdefault(section, {})[key] = value
    lines = source.splitlines(keepends=True)
    header = re.compile(r'''^\s*\[(?:([a-z_]+)|"([a-z_]+)"|'([a-z_]+)')\]\s*(?:#.*)?$''')
    start = end = None
    for i, line in enumerate(lines):
        match = header.fullmatch(line.rstrip("\r\n"))
        if match:
            if start is not None:
                end = i
                break
            if next(v for v in match.groups() if v is not None) == section:
                start = i + 1
    if start is None:
        candidate = source.rstrip("\n") + f"\n\n[{section}]\n{key} = {literal(value)}\n"
        if tomllib.loads(candidate) == expected:
            return candidate
        raise ValueError("Could not safely add a configuration section")
    end = len(lines) if end is None else end
    assignment = re.compile(r'''^(\s*)(?:''' + re.escape(key) + '|"' + re.escape(key) + '"|' + "'" + re.escape(key) + "'" + r''')\s*=''')
    for i in range(start, end):
        match = assignment.match(lines[i])
        if not match:
            continue
        # Preserve a trailing comment when the assignment is a single line.
        quote = None
        escaped = False
        comment = ""
        for position, char in enumerate(lines[i][match.end():], match.end()):
            if escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif quote:
                if char == quote:
                    quote = None
            elif char in "\"'":
                quote = char
            elif char == "#":
                comment = " " + lines[i][position:].rstrip("\r\n")
                break
        replacement = f"{match[1]}{key} = {literal(value)}{comment}\n"
        # A multiline value may span several lines; verify the complete parse
        # before accepting any replacement, so adjacent settings cannot vanish.
        for stop in range(i + 1, end + 1):
            candidate = "".join(lines[:i] + [replacement] + lines[stop:])
            try:
                if tomllib.loads(candidate) == expected:
                    return candidate
            except tomllib.TOMLDecodeError:
                pass
        raise ValueError(f"Could not safely update {section}.{key}; edit config.toml directly")
    candidate = "".join(lines[:end] + [f"{key} = {literal(value)}\n"] + lines[end:])
    if tomllib.loads(candidate) != expected:
        raise ValueError(f"Could not safely add {section}.{key}")
    return candidate


def update(content: bytes, settings: dict, directory: Path) -> bytes:
    current = decode(content, directory)
    source = content.decode("utf-8")
    for name, (section, key) in FIELDS.items():
        if name in settings and settings[name] != current.get(name):
            source = _edit(source, section, key, settings[name])
    # The UI does not edit bindings, but preserve intentional programmatic changes.
    for name, value in settings.get("keymap", {}).items():
        if value != current["keymap"].get(name, DEFAULT_BINDINGS[name]):
            source = _edit(source, "keys", name, value)
    return source.encode("utf-8")
