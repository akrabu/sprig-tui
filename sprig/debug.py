"""Opt-in private JSON logs with bounded rotation and no outline text."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import traceback

from .storage import file_lock


HEADER = b'{"sprig_debug_log": 1}\n'


class DebugLog:
    def __init__(self, *, max_bytes: int = 1024 * 1024, keep: int = 3) -> None:
        self.path: Path | None = None
        self.max_bytes, self.keep = max_bytes, keep
        self.failure = ""

    def configure(self, enabled: bool, path: str, directory: Path, *, protected=()) -> bool:
        self.failure = ""
        if not enabled:
            self.path = None
            return True
        target = Path(path).expanduser()
        if not target.is_absolute():
            target = directory / target
        target = target.absolute()
        if target.resolve() in {Path(p).resolve() for p in protected}:
            self.path = None
            self.failure = "The debug log must be different from documents and configuration files"
            return False
        self.path = target
        return self.event("logging.enabled")

    @staticmethod
    def _owned(path: Path) -> None:
        if path.is_symlink():
            raise OSError("Refusing a symbolic link as a debug log")
        try:
            if not stat.S_ISREG(path.stat().st_mode):
                raise OSError("The debug log must be a regular file")
            with path.open("rb") as stream:
                if stream.read(len(HEADER)) != HEADER:
                    raise OSError("Existing file is not a Sprig debug log; choose another filename")
        except FileNotFoundError:
            pass

    def event(self, name: str, **fields) -> bool:
        if self.path is None:
            return True
        try:
            record = {"time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                      "event": name, **fields}
            line = (json.dumps(record, ensure_ascii=True, allow_nan=False) + "\n").encode()
            self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            with file_lock(self.path):
                paths = [self.path, *(self.path.with_name(self.path.name + f".{i}") for i in range(1, self.keep + 1))]
                # Never overwrite an unrelated file, including rotation destinations.
                for path in paths:
                    self._owned(path)
                if self.path.exists() and self.path.stat().st_size + len(line) > self.max_bytes:
                    paths[-1].unlink(missing_ok=True)
                    for source, target in reversed(list(zip(paths, paths[1:]))):
                        if source.exists():
                            os.replace(source, target)
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "ab") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise OSError("The debug log must be a regular file")
                    os.fchmod(stream.fileno(), 0o600)
                    if os.fstat(stream.fileno()).st_size == 0:
                        stream.write(HEADER)
                    stream.write(line)
            return True
        except (OSError, ValueError, TypeError) as error:
            self.failure = str(error)
            self.path = None  # Logging failures must not prevent editing or recovery.
            return False

    def exception(self, operation: str, error: BaseException) -> None:
        frames = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                  for frame in traceback.extract_tb(error.__traceback__)]
        # Exception messages and locals can contain note text, queries, or paths.
        self.event("exception", operation=operation, error=type(error).__name__,
                   errno=getattr(error, "errno", None), frames=frames)


def log_cli_exception(error: BaseException, document: str) -> None:
    """Capture startup failures only when an existing config opts into logging."""
    from .config import config_directory
    from .preferences import decode
    from .state import validate_data
    from .storage import read_limited
    try:
        directory = config_directory()
        config = directory / "config.toml"
        settings = decode(read_limited(config), directory)
        validate_data(settings, settings=True)
        log = DebugLog()
        if log.configure(settings["debug_enabled"], settings["debug_path"], directory,
                         protected=(Path(document).expanduser(), config)):
            log.exception("cli", error)
    except (OSError, ValueError, RecursionError):
        pass
