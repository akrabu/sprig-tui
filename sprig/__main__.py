"""CLI entry point. Run with python3 -m sprig [file.opml]."""

import argparse
import locale
import math
import sys

from . import __version__
from .opml import OpmlFile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A Workflowy-inspired terminal outliner backed by OPML.")
    parser.add_argument("file", nargs="?", default="outline.opml", help="OPML file to open or create (default: outline.opml)")
    parser.add_argument("--no-autosave", action="store_true", help="disable automatic OPML recovery snapshots")
    parser.add_argument("--autosave-interval", "--autosave-delay", dest="autosave_interval", type=float, metavar="SECONDS",
                        help="snapshot interval (default: 60 seconds, or remembered preference)")
    parser.add_argument("--autosave-dir", metavar="DIRECTORY", help="uncompressed snapshot directory")
    parser.add_argument("--backup-dir", metavar="DIRECTORY", help="gzip OPML backup directory")
    parser.add_argument("--backup-count", type=int, metavar="COUNT", help="number of full backups to retain per file (default: 20)")
    parser.add_argument("--autosave-count", type=int, metavar="COUNT", help="number of autosave snapshots to retain per file (default: 20)")
    parser.add_argument("--no-backups", action="store_true", help="disable full OPML backups on save")
    parser.add_argument("--no-save-on-exit", action="store_true", help="offer Save / Discard / Cancel before closing unsaved files")
    parser.add_argument("--check", action="store_true", help="validate an existing OPML file and show counts without opening the TUI")
    parser.add_argument("--check-config", action="store_true", help="validate config.toml without opening the TUI")
    parser.add_argument("--version", action="version", version=f"sprig {__version__}")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):
        parser.error("Sprig requires Python 3.11 or newer")
    if args.autosave_interval is not None and (not math.isfinite(args.autosave_interval) or args.autosave_interval < 0.1):
        parser.error("--autosave-interval must be a finite number of at least 0.1 seconds")
    for name in ("backup_count", "autosave_count"):
        value = getattr(args, name)
        if value is not None and not 1 <= value <= 10000:
            parser.error("--" + name.replace("_", "-") + " must be between 1 and 10000")
    try:
        if args.check_config:
            from .state import StateStore
            store = StateStore()
            if store.blocked:
                raise ValueError("; ".join(store.blocked.values()))
            print(f"Valid configuration: {store.config_path}")
            return 0
        source, editor = OpmlFile.open(args.file)
        if args.check:
            if source.fingerprint is None:
                raise FileNotFoundError(args.file)
            count = notes = completed = depth = 0
            stack = [(n, 1) for n in editor.root.children]
            while stack:
                node, level = stack.pop()
                count += 1
                notes += "_note" in node.attrs
                completed += node.completed
                depth = max(depth, level)
                stack.extend((n, level + 1) for n in node.children)
            print(f"{count} bullets, {notes} notes, {completed} completed, depth {depth}")
            return 0
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            parser.error("the editor needs an interactive terminal; use --check to validate OPML")
        try:
            import curses
        except ImportError:
            print("sprig: this editor requires Python with curses support (Linux or macOS).", file=sys.stderr)
            return 1
        from .ui import App
        locale.setlocale(locale.LC_ALL, "")
        try:
            options = {"backup_directory": args.backup_dir, "autosave_directory": args.autosave_dir,
                       "backup_count": args.backup_count, "autosave_count": args.autosave_count,
                       "backups_enabled": False if args.no_backups else None,
                       "save_on_exit": False if args.no_save_on_exit else None}
            curses.wrapper(lambda screen: App(screen, source, editor, autosave=False if args.no_autosave else None,
                                              autosave_delay=args.autosave_interval, storage_options=options).run())
        except curses.error as error:
            print(f"sprig: terminal error: {error}", file=sys.stderr)
            return 1
    except (OSError, ValueError) as error:
        from .debug import log_cli_exception
        log_cli_exception(error, args.file)
        print(f"sprig: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        from .debug import log_cli_exception
        log_cli_exception(error, args.file)
        raise
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
