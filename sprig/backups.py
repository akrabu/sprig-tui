"""Gzip full backups and plain OPML autosaves, with checksums and retention."""

from datetime import datetime, timedelta
import gzip
import hashlib
import io
from pathlib import Path
import re
import zlib

from .config import config_directory
from .storage import MAX_DOCUMENT_BYTES, atomic_write, file_lock, read_limited, sync_directory


class Backups:
    def __init__(self, source: Path, keep: int = 20, directory: str | Path | None = None,
                 *, autosave: bool = False) -> None:
        self.source = source.resolve()
        self.keep = max(1, keep)
        self.autosave = autosave
        self.extension = ".opml" if autosave else ".opml.gz"
        base = Path(directory).expanduser() if directory else config_directory() / ("autosave" if autosave else "backup")
        if not base.is_absolute():
            base = self.source.parent / base
        key = hashlib.sha256(str(self.source).encode()).hexdigest()[:16]
        self.directory = base.resolve() / (self.source.name + "-" + key)

    def create(self, previous: bytes) -> Path:
        if len(previous) > MAX_DOCUMENT_BYTES:
            raise ValueError("Snapshot exceeds 64 MiB")
        content = previous if self.autosave else gzip.compress(previous, compresslevel=6, mtime=0)
        if not self.autosave and gzip.decompress(content) != previous:
            raise OSError("Backup compression verification failed")
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        with file_lock(self.directory / "catalog"):
            return self._write(content)

    def _write(self, content: bytes) -> Path:
        paths = self.paths()
        if paths:
            latest = paths[-1]
            try:
                checksum = read_limited(latest.with_name(latest.name + ".sha256"), 4096).decode("ascii").split()
                if (checksum and checksum[0] == hashlib.sha256(content).hexdigest()
                        and read_limited(latest) == content):
                    return latest
            except (OSError, ValueError):
                pass
        stamp = datetime.now().replace(microsecond=0)
        if paths:
            previous_stamp = paths[-1].name[len(self.source.stem) + 1:-len(self.extension)]
            try:
                stamp = max(stamp, datetime.strptime(previous_stamp, "%Y-%m-%d_%H%M%S") + timedelta(seconds=1))
            except ValueError:
                pass
        while True:
            name = f"{self.source.stem}_{stamp:%Y-%m-%d_%H%M%S}" + self.extension
            path = self.directory / name
            if not path.exists() and not path.with_name(name + ".sha256").exists():
                break
            stamp += timedelta(seconds=1)
        atomic_write(path, content)
        if read_limited(path) != content:
            raise OSError("Backup verification failed; original file was not replaced")
        checksum = hashlib.sha256(content).hexdigest() + "  " + name + "\n"
        atomic_write(path.with_name(name + ".sha256"), checksum.encode())
        return path

    def prune(self) -> None:
        if not self.directory.exists():
            return
        with file_lock(self.directory / "catalog"):
            self._prune()

    def _prune(self) -> None:
        paths = self.paths()
        # Verify all snapshots we will keep before removing an older version.
        for path in paths[-self.keep:]:
            self.read(path)
        for path in paths[:-self.keep]:
            # Never remove the last valid versions if a snapshot is damaged.
            self.read(path)
            path.unlink()
            path.with_name(path.name + ".sha256").unlink()
        sync_directory(self.directory)

    def paths(self) -> list[Path]:
        pattern = re.compile(re.escape(self.source.stem) + r"_\d{4}-\d{2}-\d{2}_\d{6}" + re.escape(self.extension) + "$")
        return sorted(p for p in self.directory.glob("*" + self.extension)
                      if pattern.fullmatch(p.name) and p.with_name(p.name + ".sha256").is_file())

    @staticmethod
    def read(path: Path) -> bytes:
        if path.suffix.lower() not in (".opml", ".gz"):
            raise ValueError("Choose an .opml or .opml.gz snapshot. Older backups have been preserved.")
        data = read_limited(path)
        fields = read_limited(path.with_name(path.name + ".sha256"), 4096).decode("ascii").split()
        if not fields or fields[0] != hashlib.sha256(data).hexdigest():
            raise ValueError("Backup checksum mismatch")
        if path.suffix.lower() == ".gz":
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                    content = stream.read(MAX_DOCUMENT_BYTES + 1)
                if len(content) > MAX_DOCUMENT_BYTES:
                    raise ValueError("Decompressed backup exceeds 64 MiB")
                return content
            except (OSError, EOFError, zlib.error) as error:
                raise ValueError(f"Invalid gzip backup: {error}") from error
        return data
