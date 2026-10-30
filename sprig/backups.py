"""Verified Brotli snapshots, with bounded retention and collision-safe names."""

from datetime import datetime, timedelta
import hashlib
from pathlib import Path
import re

from .config import config_directory
from .storage import MAX_DOCUMENT_BYTES, atomic_write, file_lock, read_limited, sync_directory


def codec():
    try:
        import brotli
    except ImportError as error:
        raise OSError("Brotli is required for backups. Install Sprig with 'pip install -e .'.") from error
    return brotli


class Backups:
    def __init__(self, source: Path, keep: int = 20, directory: str | Path | None = None,
                 *, compressed: bool = True) -> None:
        self.source = source.resolve()
        self.keep = max(1, keep)
        self.compressed = compressed
        self.extension = ".opml.br" if compressed else ".opml"
        base = Path(directory).expanduser() if directory else config_directory() / ("backup" if compressed else "autosave")
        if not base.is_absolute():
            base = self.source.parent / base
        key = hashlib.sha256(str(self.source).encode()).hexdigest()[:16]
        self.directory = base.resolve() / (self.source.name + "-" + key)

    def create(self, previous: bytes) -> Path:
        if len(previous) > MAX_DOCUMENT_BYTES:
            raise ValueError("Snapshot exceeds 64 MiB")
        compressed = previous
        if self.compressed:
            brotli = codec()
            try:
                compressed = brotli.compress(previous, mode=brotli.MODE_TEXT, quality=4)
                if brotli.decompress(compressed) != previous:
                    raise ValueError("Brotli round-trip verification failed")
            except (brotli.error, ValueError) as error:
                raise OSError(f"Backup compression failed: {error}") from error
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        with file_lock(self.directory / "catalog"):
            return self._write(compressed)

    def _write(self, compressed: bytes) -> Path:
        paths = self.paths()
        if paths:
            latest = paths[-1]
            try:
                checksum = read_limited(latest.with_name(latest.name + ".sha256"), 4096).decode("ascii").split()
                if (checksum and checksum[0] == hashlib.sha256(compressed).hexdigest()
                        and read_limited(latest) == compressed):
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
        atomic_write(path, compressed)
        checksum = hashlib.sha256(compressed).hexdigest() + "  " + name + "\n"
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
        data = read_limited(path)
        fields = read_limited(path.with_name(path.name + ".sha256"), 4096).decode("ascii").split()
        if not fields or fields[0] != hashlib.sha256(data).hexdigest():
            raise ValueError("Backup checksum mismatch")
        if path.suffix != ".br":
            return data
        brotli = codec()
        decoder = brotli.Decompressor()
        output = bytearray()
        try:
            for offset in range(0, len(data), 4096):
                # Bound output even when given a corrupt or hostile stream.
                pending = data[offset:offset + 4096]
                while True:
                    output.extend(decoder.process(pending, output_buffer_limit=MAX_DOCUMENT_BYTES - len(output) + 1))
                    if len(output) > MAX_DOCUMENT_BYTES:
                        raise ValueError("Decompressed backup exceeds 64 MiB")
                    if decoder.can_accept_more_data():
                        break
                    pending = b""
            if not decoder.is_finished():
                raise ValueError("Truncated Brotli backup")
        except brotli.error as error:
            raise ValueError(f"Invalid Brotli backup: {error}") from error
        return bytes(output)
