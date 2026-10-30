"""TOML migration, configurable commands, safe logging, and gzip integrity."""

import gzip
import curses
import io
import os
from pathlib import Path
import stat
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from sprig.backups import Backups
from sprig.debug import DebugLog
from sprig.keymap import KeyMap
from sprig.keys import Event, Keyboard, decode_sequence
from sprig.model import Node
from sprig.opml import OpmlFile
from sprig.preferences import encode
from sprig.state import StateStore
from sprig.storage import digest
from sprig.ui import App
from tests.test_features import Screen


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.config = self.directory / "config"
        environment = patch.dict(os.environ, {"SPRIG_CONFIG_DIR": str(self.config)})
        environment.start()
        self.addCleanup(environment.stop)
        self.path = self.directory / "notes.opml"

    def app(self):
        source, editor = OpmlFile.open(self.path)
        editor.change(lambda: editor.insert("Parent"))
        editor.change(lambda: editor.selected.add(Node({"text": "Child"})))
        source.save(editor)
        app = App(Screen(), source, editor, storage_options={"save_on_exit": False})
        self.addCleanup(app.recovery.close)
        return app

    def configure(self, app, **values):
        settings = {**app.store.settings, **values}
        app.store.config_path.write_bytes(encode(settings))
        app.reload_config()
        self.assertFalse(app.store.blocked)

    def test_default_config_has_paths_options_and_editable_keymap(self):
        store = StateStore()
        data = tomllib.loads(store.config_path.read_text())
        self.assertEqual(data["autosave"]["interval_seconds"], 60)
        self.assertEqual(data["backup"]["directory"], str(self.config / "backup"))
        self.assertFalse(data["debug"]["enabled"])
        self.assertEqual(data["debug"]["path"], str(self.config / "debug.log"))
        self.assertIn("ctrl+,", data["keys"]["collapse"])
        self.assertIn("ctrl+.", data["keys"]["expand"])
        self.assertFalse((self.config / "debug.log").exists())

    def test_menu_updates_keep_comments_and_multiline_bindings(self):
        store = StateStore()
        store.config_path.write_text('''# my custom settings
version = 1
[backup]
keep = 20 # retain this comment
directory = "custom backup"
[keys]
collapse = [
    "alt+c", # preferred key
]
expand = "alt+e"
''')
        self.assertTrue(store.reload_settings())
        store.settings["backup_count"] = 5
        store.settings["search_scope"] = "Global"
        store.save_settings()
        text = store.config_path.read_text()
        self.assertIn("# my custom settings", text)
        self.assertIn("keep = 5 # retain this comment", text)
        self.assertIn('    "alt+c", # preferred key', text)
        data = tomllib.loads(text)
        self.assertEqual(data["keys"]["collapse"], ["alt+c"])
        self.assertEqual(data["search"]["scope"], "Global")

    def test_failed_toml_migration_keeps_valid_manual_save_preferences(self):
        self.config.mkdir()
        legacy = self.config / "settings.json"
        legacy.write_text('{"version": 1, "save_on_exit": false, "backup_directory": "custom-archive"}')
        original = legacy.read_bytes()
        with patch.object(StateStore, "write_bytes", side_effect=OSError("read-only config directory")):
            store = StateStore()
        self.assertFalse(store.settings["save_on_exit"])
        self.assertEqual(store.settings["backup_directory"], "custom-archive")
        self.assertTrue(store.blocked)
        self.assertFalse(store.config_path.exists())
        self.assertEqual(legacy.read_bytes(), original)

    def test_invalid_config_is_preserved_and_reload_keeps_active_bindings(self):
        app = self.app()
        for content in ('[broken', 'version = 2', '[backup]\nkeep = 0',
                        '[debug]\nenabled = "yes"', '[keys]\ncollapse = ["ctrl+d"]'):
            app.store.config_path.write_text(content)
            app.reload_config()
            self.assertTrue(app.store.blocked)
            self.assertEqual(app.keymap.command(Event("ctrl+,")), "collapse")
            with self.assertRaises(OSError):
                app.store.save_settings()
            self.assertEqual(app.store.config_path.read_text(), content)
        self.configure(app, keymap={"collapse": ["alt+c"]})
        self.assertEqual(app.keymap.command(Event("alt+c")), "collapse")

    def test_remapping_disables_previous_keys_and_updates_menu_and_help(self):
        app = self.app()
        self.configure(app, keymap={"collapse": ["alt+c"], "expand": ["alt+e"], "duplicate": ["ctrl+g"]})
        app.handle(Event("ctrl+up"))
        self.assertFalse(app.editor.collapsed)
        app.handle(Event("alt+c"))
        self.assertIn(app.editor.selected.uid, app.editor.collapsed)
        app.handle(Event("alt+e"))
        self.assertFalse(app.editor.collapsed)
        app.handle(Event("ctrl+d"))
        self.assertEqual(len(app.editor.root.children), 1)
        app.handle(Event("ctrl+g"))
        self.assertEqual(len(app.editor.root.children), 2)
        self.assertEqual(app.menus()[1][1][1][1], "Ctrl+G")
        self.assertTrue(any("Alt+C" in line for line in app.keymap.help_lines()))

    def test_quit_mapping_and_menu_quit_are_independent_of_default_key(self):
        app = self.app()
        self.configure(app, keymap={"quit": ["alt+q"]})
        app.handle(Event("ctrl+q"))
        self.assertTrue(app.running)
        app.handle(Event("escape"))
        app.menus()[0][1][-1][2]()
        self.assertFalse(app.running)

    def test_punctuation_folding_and_older_xterm_cursor_sequences(self):
        app = self.app()
        for collapse, expand in (("[44;5u", "[46;5u"), ("[44;6u", "[46;6u"),
                                 ("[60;5u", "[62;5u"), ("[27;5;44~", "[27;5;46~"),
                                 ("[5A", "[5B"), ("[>1;5A", "[>1;5B")):
            app.handle(decode_sequence(collapse))
            self.assertEqual(len(app.editor.visible()), 1)
            app.handle(decode_sequence(expand))
            self.assertEqual(len(app.editor.visible()), 2)
        app.handle(Event("escape"))
        app.handle(Event("ctrl+,"))
        self.assertFalse(app.menu_open)
        self.assertEqual(len(app.editor.visible()), 1)

    def test_key_conflicts_invalid_names_and_disabled_bindings(self):
        for keys in ({"collapse": "ctrl+d"}, {"unknown_command": "alt+z"}, {"collapse": [1]}, {"collapse": ["j"]}):
            with self.assertRaises(ValueError):
                KeyMap(keys)
        keys = KeyMap({"duplicate": [], "collapse": ["CTRL+d"]})
        self.assertEqual(keys.translate(Event("ctrl+d")).key, "ctrl+,")
        self.assertEqual(keys.translate(Event("ctrl+up")).key, "unbound")

    def test_extended_function_keys_can_be_mapped(self):
        app = self.app()
        self.configure(app, keymap={"collapse": ["f13"], "expand": ["ctrl+f1"]})
        app.handle(Keyboard(Screen()).normalize(curses.KEY_F0 + 13))
        self.assertEqual(len(app.editor.visible()), 1)
        app.handle(decode_sequence("[1;5P"))
        self.assertEqual(len(app.editor.visible()), 2)

    def test_debug_is_off_by_default_and_can_use_a_custom_filename(self):
        app = self.app()
        app.handle(Event("f9"))
        self.assertFalse((self.config / "debug.log").exists())
        self.configure(app, debug_enabled=True, debug_path="logs/session.txt")
        self.assertTrue((self.config / "logs" / "session.txt").is_file())
        self.configure(app, debug_enabled=False)
        path = self.config / "logs" / "session.txt"
        before = path.read_bytes()
        app.handle(Event("f9"))
        self.assertEqual(path.read_bytes(), before)

    def test_debug_redacts_note_text_and_exception_messages(self):
        app = self.app()
        self.configure(app, debug_enabled=True)
        app.keyboard.last_sequence = "[text redacted]"
        app.handle(Event("text", "PRIVATE_NOTE_CONTENT"))
        self.path.write_text("PRIVATE_EXTERNAL_DATA")
        self.assertFalse(app.save())
        text = (self.config / "debug.log").read_text()
        self.assertNotIn("PRIVATE_NOTE_CONTENT", text)
        self.assertNotIn("PRIVATE_EXTERNAL_DATA", text)
        self.assertNotIn(str(self.directory), text)
        self.assertIn("FileChangedError", text)
        self.assertIn('"event": "input"', text)

    def test_unhandled_crash_is_logged_and_re_raised(self):
        app = self.app()
        self.configure(app, debug_enabled=True)
        with patch.object(app, "setup"), patch.object(app, "draw"), \
             patch.object(app.keyboard, "read", side_effect=RuntimeError("PRIVATE_EXCEPTION_TEXT")), \
             patch("sprig.ui.sys.stdout", new=io.StringIO()):
            with self.assertRaises(RuntimeError):
                app.run()
        text = (self.config / "debug.log").read_text()
        self.assertIn("RuntimeError", text)
        self.assertIn("editor.run", text)
        self.assertNotIn("PRIVATE_EXCEPTION_TEXT", text)

    def test_debug_rotation_and_protection_of_unrelated_files(self):
        path = self.directory / "debug.log"
        log = DebugLog(max_bytes=300, keep=2)
        self.assertTrue(log.configure(True, str(path), self.config))
        for _ in range(20):
            self.assertTrue(log.event("test.event", code="example"))
        self.assertEqual(len(list(self.directory.glob("debug.log*"))), 3)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        note = self.directory / "keep.txt"
        note.write_text("PRIVATE_NOTE")
        self.assertFalse(log.configure(True, str(note), self.config))
        self.assertEqual(note.read_text(), "PRIVATE_NOTE")
        self.assertFalse(log.configure(True, str(self.path), self.config, protected=(self.path,)))
        self.assertFalse(self.path.exists())

    def test_gzip_archives_are_standard_and_decompression_is_bounded(self):
        manager = Backups(self.path)
        content = b'<opml><body><outline text="snapshot"/></body></opml>'
        path = manager.create(content)
        self.assertTrue(path.name.endswith(".opml.gz"))
        self.assertEqual(gzip.decompress(path.read_bytes()), content)
        self.assertEqual(manager.read(path), content)
        with patch("sprig.backups.MAX_DOCUMENT_BYTES", 16):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                manager.read(path)
        broken = b"not a gzip archive"
        path.write_bytes(broken)
        path.with_name(path.name + ".sha256").write_text(digest(broken))
        with self.assertRaises(ValueError):
            manager.read(path)

    def test_logging_rejects_unrelated_rotation_files_symlinks_and_pipes(self):
        path = self.directory / "debug.log"
        log = DebugLog(max_bytes=200, keep=2)
        self.assertTrue(log.configure(True, str(path), self.config))
        before = path.read_bytes()
        rotated = path.with_name(path.name + ".1")
        rotated.write_text("UNRELATED_DATA")
        self.assertFalse(log.event("example"))
        self.assertEqual(rotated.read_text(), "UNRELATED_DATA")
        self.assertEqual(path.read_bytes(), before)
        link = self.directory / "log-link"
        link.symlink_to(rotated)
        self.assertFalse(log.configure(True, str(link), self.config))
        pipe = self.directory / "log-pipe"
        os.mkfifo(pipe)
        self.assertFalse(log.configure(True, str(pipe), self.config))

    def test_rotation_keeps_archives_in_other_formats(self):
        manager = Backups(self.path, 1)
        manager.create(b"one")
        other = manager.directory / "older-archive.saved"
        other.write_bytes(b"retain this unrelated archive")
        manager.create(b"two")
        manager.prune()
        self.assertEqual(other.read_bytes(), b"retain this unrelated archive")
        self.assertEqual(len(manager.paths()), 1)

    def test_unchanged_view_is_not_rewritten(self):
        app = self.app()
        app.persist_view()
        path = app.store.file_path(app.source.path)
        before = path.stat().st_mtime_ns
        with patch("sprig.state.OpmlFile._atomic", side_effect=AssertionError("unexpected write")):
            app.persist_view()
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_empty_recovery_cleanup_keeps_other_sessions_and_skips_sync(self):
        app = self.app()
        app.recovery.directory.mkdir()
        other = app.recovery.directory / "other-session.json"
        other.write_text("preserve another session")
        with patch("sprig.recovery.sync_directory", side_effect=AssertionError("unexpected directory sync")):
            app.recovery.clear()
        self.assertEqual(other.read_text(), "preserve another session")


if __name__ == "__main__":
    unittest.main()
