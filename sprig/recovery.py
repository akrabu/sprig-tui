"""Private, checksummed unsaved snapshots; live sessions hold advisory locks."""

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
from uuid import uuid4

from .opml import OpmlFile
from .state import validate_data
from .storage import atomic_write, digest, read_limited, sync_directory


class Recovery:
    def __init__(self, directory: Path) -> None:
        self.directory = directory / "recovery"
        self.path = self.directory / (uuid4().hex + ".json")
        self.fd: int | None = None
        self.claimed: dict[Path, int] = {}
        self.last = None

    @staticmethod
    def lock(path: Path) -> int:
        fd = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def read(path: Path) -> dict:
        try:
            data = json.loads(read_limited(path, 128 * 1024 * 1024))
        except RecursionError as error:
            raise ValueError("Recovery metadata is nested too deeply") from error
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("Unsupported recovery format")
        for name in ("file", "opml", "checksum", "saved_at"):
            if not isinstance(data.get(name), str):
                raise ValueError(f"Invalid recovery {name}")
        base = data.get("base_fingerprint")
        if base is not None and (not isinstance(base, str) or len(base) != 64):
            raise ValueError("Invalid recovery file fingerprint")
        if digest(data["opml"].encode("utf-8")) != data["checksum"]:
            raise ValueError("Recovery checksum mismatch; original snapshot preserved")
        validate_data(data.get("view", {}))
        return data

    def pending(self, source_path: Path | None = None) -> tuple[list[tuple[Path, dict]], list[str]]:
        found, errors = [], []
        for path in sorted(self.directory.glob("*.json")):
            if path == self.path or path in self.claimed:
                continue
            try:
                fd = self.lock(path)
            except BlockingIOError:
                continue
            except OSError as error:
                errors.append(f"{path.name}: {error}")
                continue
            try:
                data = self.read(path)
                if source_path is None or Path(data["file"]) == source_path:
                    found.append((path, data))
            except (OSError, ValueError) as error:
                errors.append(f"{path.name}: {error}")
            finally:
                os.close(fd)
        found.sort(key=lambda record: record[1]["saved_at"], reverse=True)
        return found, errors

    def write(self, source, editor, store) -> None:
        key = (id(editor), editor.revision, source.path, source.fingerprint,
               frozenset(editor.stars), frozenset(editor.collapsed), editor.zoom.uid,
               editor.selected.uid, editor.cursor, editor.field, frozenset(editor.filter_labels))
        if self.last == key:
            return
        content = source.encode(editor)
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.fd is None:
            self.fd = self.lock(self.path)
        data = {"version": 1, "file": str(source.path), "base_fingerprint": source.fingerprint,
                "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "opml": content.decode("utf-8"), "checksum": digest(content),
                "view": store.view_data(source, editor, current=True)}
        encoded = (json.dumps(data, ensure_ascii=False) + "\n").encode()
        if len(encoded) > 128 * 1024 * 1024:
            raise ValueError("Recovery snapshot exceeds 128 MiB")
        atomic_write(self.path, encoded)
        self.last = key

    def load(self, path: Path):
        fd = self.lock(path)
        try:
            data = self.read(path)
            source, editor = OpmlFile.decode(data["file"], data["opml"].encode("utf-8"))
            source.fingerprint = data["base_fingerprint"]
        except BaseException:
            os.close(fd)
            raise
        self.claimed[path] = fd
        return source, editor, data["view"]

    def clear(self) -> None:
        removed = False
        for path in [self.path, *self.claimed]:
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                pass
        if removed:
            sync_directory(self.directory)
        for fd in self.claimed.values():
            os.close(fd)
        self.claimed.clear()
        self.last = None

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        for fd in self.claimed.values():
            os.close(fd)
        self.claimed.clear()
