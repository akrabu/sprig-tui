"""Configuration locations shared by the UI and storage layer."""

import os
from pathlib import Path


def config_directory() -> Path:
    configured = os.environ.get("SPRIG_CONFIG_DIR") or os.environ.get("OUTLINER_CONFIG_DIR")
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return Path(configured or base / "sprig-tui").expanduser().resolve()


def legacy_directory() -> Path | None:
    if os.environ.get("SPRIG_CONFIG_DIR") or os.environ.get("OUTLINER_CONFIG_DIR"):
        return None
    return (Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "outliner-tui").expanduser().resolve()
