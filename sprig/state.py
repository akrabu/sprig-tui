"""Central UI metadata; never adds private attributes to an OPML document."""

from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path

from .model import Editor, Node
from .opml import OpmlFile
from .config import config_directory, legacy_directory
from .storage import CommittedWriteError, digest, file_lock, read_limited
from . import preferences
from .keymap import KeyMap


def node_hash(node: Node) -> str:
    return hashlib.sha256(json.dumps(node.attrs, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def references(editor: Editor, include_paths: bool = True) -> dict[str, dict]:
    result = {}
    stack = [(editor.root, None, 0, "")]
    while stack:
        node, parent_uid, index, parent_context = stack.pop()
        fingerprint = node_hash(node)
        context = hashlib.sha256((parent_context + fingerprint).encode()).hexdigest()
        result[node.uid] = {"uid": node.uid, "_parent": parent_uid, "_index": index, "hash": fingerprint,
                            "context": context, "label": node.text[:100]}
        stack.extend((child, node.uid, i, context) for i, child in reversed(list(enumerate(node.children))))
    return {uid: materialize(uid, result) for uid in result} if include_paths else result


def materialize(uid: str, refs: dict[str, dict]) -> dict:
    result = {k: v for k, v in refs[uid].items() if not k.startswith("_")}
    path = []
    ref = refs[uid]
    while ref.get("_parent") is not None:
        path.append(ref["_index"])
        ref = refs[ref["_parent"]]
    result["path"] = list(reversed(path))
    return result


def resolve(editor: Editor, ref: dict) -> Node | None:
    if not isinstance(ref, dict):
        return None
    current = references(editor, include_paths=False)
    context = ref.get("context")
    matches = [n for n in editor.root.walk() if context and current[n.uid]["context"] == context]
    if len(matches) == 1:
        return matches[0]
    matches = [n for n in editor.root.walk() if node_hash(n) == ref.get("hash")]
    return matches[0] if len(matches) == 1 else None


def validate_data(data: dict, *, settings: bool = False) -> None:
    if (not isinstance(data, dict) or type(data.get("version", 1)) is not int
            or data.get("version", 1) != 1):
        raise ValueError("unsupported configuration format/version")
    strings = ("search_scope", "last_directory", "backup_directory", "autosave_directory", "debug_path") if settings else ("file", "selected", "zoom", "field")
    for key in strings:
        if key in data and not isinstance(data[key], str):
            raise ValueError(f"invalid {key}")
    if settings:
        for key in ("backup_count", "autosave_count"):
            if key in data and (type(data[key]) is not int or not 1 <= data[key] <= 10000):
                raise ValueError(f"invalid {key}")
        if "autosave_interval" in data:
            value = data["autosave_interval"]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0.1:
                raise ValueError("invalid autosave_interval")
        for key in ("autosave_enabled", "backups_enabled", "save_on_exit", "debug_enabled"):
            if key in data and type(data[key]) is not bool:
                raise ValueError(f"invalid {key}")
        if data.get("search_scope", "Whole file") not in ("Current branch", "Whole file", "Global"):
            raise ValueError("invalid search_scope")
        for key in ("backup_directory", "autosave_directory", "last_directory", "debug_path"):
            if any(c in data.get(key, "") for c in "\x00\r\n"):
                raise ValueError(f"invalid {key}")
        if "debug_path" in data and not data["debug_path"].strip():
            raise ValueError("debug_path must include a filename")
        KeyMap(data.get("keymap", {}))
        return
    if data.get("field", "text") not in ("text", "_note"):
        raise ValueError("invalid field")
    if type(data.get("cursor", 0)) is not int or data.get("cursor", 0) < 0:
        raise ValueError("invalid cursor")
    for key in ("stars", "collapsed", "filters"):
        if not isinstance(data.get(key, []), list) or any(not isinstance(v, str) for v in data.get(key, [])):
            raise ValueError(f"invalid {key}")
    if type(data.get("show_completed", True)) is not bool:
        raise ValueError("invalid show_completed")
    if not isinstance(data.get("refs", []), list):
        raise ValueError("invalid references")
    used = set()
    for ref in data.get("refs", []):
        if not isinstance(ref, dict) or any(not isinstance(ref.get(k), str) for k in ("uid", "hash", "context", "label")):
            raise ValueError("invalid node reference")
        path = ref.get("path")
        if not isinstance(path, list) or any(type(i) is not int or i < 0 for i in path):
            raise ValueError("invalid node path")
        if ref["uid"] in used or ((ref["uid"] == "root") != (not path)):
            raise ValueError("duplicate or invalid node identity")
        used.add(ref["uid"])


class StateStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory or config_directory()).expanduser().resolve()
        self.legacy = None if directory else legacy_directory()
        self.blocked: dict[Path, str] = {}
        self.fingerprints: dict[Path, str | None] = {}
        self.config_path = self.directory / "config.toml"
        self.settings = preferences.defaults(self.directory)
        self.reload_settings()
        self.baseline: dict[str, dict] = {}
        self.unresolved: list[dict] = []

    def file_path(self, path: Path) -> Path:
        key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
        return self.directory / "files" / (key + ".json")

    def read(self, path: Path, *, remember: bool = True) -> dict:
        try:
            try:
                content = read_limited(path)
                if remember:
                    self.fingerprints[path] = digest(content)
            except FileNotFoundError:
                if remember:
                    self.fingerprints[path] = None
                if self.legacy is None:
                    return {}
                content = read_limited(self.legacy / path.relative_to(self.directory))
            data = json.loads(content)
            validate_data(data, settings=path.name == "settings.json")
            return data
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, RecursionError) as error:
            self.blocked[path] = str(error)
            return {}

    def write(self, path: Path, data: dict) -> None:
        if path not in self.fingerprints:
            self.read(path)
        validate_data(data, settings=path.name == "settings.json")
        content = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
        self.write_bytes(path, content)

    def write_bytes(self, path: Path, content: bytes) -> None:
        if path in self.blocked:
            raise OSError(f"Settings preserved without changes: {path}: {self.blocked[path]}")
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path):
            def verify():
                try:
                    latest = read_limited(path)
                except FileNotFoundError:
                    latest = None
                current = digest(latest)
                if current != self.fingerprints[path]:
                    raise OSError(f"Preferences changed in another session: {path.name}; existing file preserved")
                return latest
            if verify() == content:
                return
            try:
                OpmlFile._atomic(path, content, before_replace=verify)
            except CommittedWriteError:
                self.fingerprints[path] = digest(content)
                raise
            self.fingerprints[path] = digest(content)

    def save_settings(self) -> None:
        validate_data(self.settings, settings=True)
        path = self.config_path
        if path in self.blocked:
            raise OSError(f"Config preserved: {self.blocked[path]}")
        try:
            previous = read_limited(path)
        except FileNotFoundError:
            previous = None
        if digest(previous) != self.fingerprints.get(path):
            raise OSError("Config changed in another session or editor; reload config.toml first")
        content = (preferences.encode(self.settings) if previous is None else
                   preferences.update(previous, self.settings, self.directory))
        if content != previous:
            self.write_bytes(path, content)

    def reload_settings(self) -> bool:
        path = self.config_path
        previous = self.settings
        try:
            try:
                content = read_limited(path)
            except FileNotFoundError:
                self.fingerprints[path] = None
                legacy_path = self.directory / "settings.json"
                legacy = self.read(legacy_path)
                if legacy_path in self.blocked:
                    raise ValueError("Legacy settings preserved: " + self.blocked[legacy_path])
                values = {**preferences.defaults(self.directory), **legacy}
                validate_data(values, settings=True)
                self.blocked.pop(path, None)
                self.settings = values
                try:
                    self.save_settings()
                except (OSError, ValueError) as error:
                    # A failed migration write must not discard valid preferences,
                    # particularly manual-save mode or a chosen backup directory.
                    self.blocked[path] = str(error)
                    return False
                return True
            values = preferences.decode(content, self.directory)
            validate_data(values, settings=True)
            self.fingerprints[path] = digest(content)
            self.blocked.pop(path, None)
            self.settings = values
            return True
        except (OSError, ValueError, RecursionError) as error:
            self.blocked[path] = str(error)
            self.settings = previous
            return False

    def bind(self, source: OpmlFile, editor: Editor, data: dict | None = None) -> None:
        data = self.read(self.file_path(source.path)) if data is None else data
        validate_data(data)
        current = references(editor, include_paths=False)
        by_hash = defaultdict(list)
        by_context = defaultdict(list)
        nodes = {n.uid: n for n in editor.root.walk()}
        for uid, ref in current.items():
            by_hash[ref["hash"]].append(uid)
            by_context[ref["context"]].append(uid)
        used = set()
        used_nodes = set()
        self.unresolved = []
        same_data = data.get("fingerprint") == source.fingerprint
        for ref in data.get("refs", []):
            uid = None
            if same_data:
                node = editor.root
                try:
                    for index in ref.get("path", []):
                        node = node.children[index]
                    uid = node.uid if node.uid in current else None
                except (IndexError, TypeError):
                    pass
            if uid is None or current[uid]["hash"] != ref.get("hash"):
                matches = by_context.get(ref.get("context"), [])
                if len(matches) != 1:
                    matches = by_hash.get(ref.get("hash"), [])
                uid = matches[0] if len(matches) == 1 else None
            old_uid = ref.get("uid")
            if uid is not None and ((old_uid == "root") != (nodes[uid] is editor.root)):
                uid = None
            if uid is not None and old_uid and old_uid not in used and id(nodes[uid]) not in used_nodes:
                used_nodes.add(id(nodes[uid]))
                nodes[uid].uid = old_uid
                used.add(old_uid)
            elif old_uid in data.get("stars", []):
                self.unresolved.append(ref)
        nodes = {n.uid: n for n in editor.root.walk()}
        editor.stars = set(data.get("stars", [])) & nodes.keys()
        editor.collapsed = set(data.get("collapsed", [])) & nodes.keys()
        editor.zoom = nodes.get(data.get("zoom"), editor.root)
        editor.select(nodes.get(data.get("selected"), editor.selected), data.get("cursor", 0), data.get("field", "text"))
        editor.show_completed = data.get("show_completed", True)
        editor.filter_labels = set(data.get("filters", []))
        editor.ensure_selection()
        editor.mark_saved()  # Restoring private identities does not edit OPML.
        self.capture(editor)

    def capture(self, editor: Editor) -> None:
        self.baseline = references(editor, include_paths=False)

    def view_data(self, source: OpmlFile, editor: Editor, *, current: bool = False) -> dict:
        existing = {n.uid for n in editor.root.walk()}
        wanted = (editor.stars | editor.collapsed | {editor.zoom.uid, editor.selected.uid}) & existing
        baseline = references(editor, include_paths=False) if current else self.baseline
        refs = [materialize(uid, baseline) for uid in sorted(wanted) if uid in baseline]
        refs += self.unresolved
        stars = sorted((editor.stars & existing) | {ref["uid"] for ref in self.unresolved})
        return {
            "version": 1, "file": str(source.path), "fingerprint": source.fingerprint,
            "refs": refs, "stars": stars,
            "collapsed": sorted(editor.collapsed & existing), "zoom": editor.zoom.uid,
            "selected": editor.selected.uid, "cursor": editor.cursor, "field": editor.field,
            "show_completed": editor.show_completed, "filters": sorted(editor.filter_labels),
        }

    def save_view(self, source: OpmlFile, editor: Editor) -> None:
        self.write(self.file_path(source.path), self.view_data(source, editor))

    def starred_files(self) -> list[dict]:
        result = []
        names = {p.name for p in (self.directory / "files").glob("*.json")}
        if self.legacy:
            names |= {p.name for p in (self.legacy / "files").glob("*.json")}
        for name in sorted(names):
            path = self.directory / "files" / name
            data = self.read(path, remember=False)
            for ref in data.get("refs", []):
                if ref.get("uid") in data.get("stars", []) and data.get("file"):
                    result.append({"file": data["file"], "ref": ref})
        return result
