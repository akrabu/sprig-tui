"""Fault injection and behavior checks for the paths that protect user data."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sprig.backups import Backups
from sprig.exports import save_export
from sprig.keys import Event, decode_sequence
from sprig.model import Node
from sprig.opml import FileChangedError, OpmlError, OpmlFile
from sprig.search import Index, fuzzy
from sprig.state import StateStore
from sprig.storage import CommittedWriteError, FileBusyError, digest, file_lock
from sprig.ui import App
from tests.test_features import Screen


class StorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.config = self.directory / "config"
        environment = patch.dict(os.environ, {"SPRIG_CONFIG_DIR": str(self.config)})
        environment.start()
        self.addCleanup(environment.stop)
        self.path = self.directory / "notes.opml"

    def app(self, **options):
        source, editor = OpmlFile.open(self.path)
        if source.fingerprint is None:
            editor.change(lambda: editor.insert("First #work @Sky"))
            editor.change(editor.enter)
            editor.change(lambda: editor.insert("Second"))
            source.save(editor)
            editor.select(editor.root.children[0])
        app = App(Screen(), source, editor, storage_options={"save_on_exit": False, **options})
        self.addCleanup(app.recovery.close)
        return app

    def test_defaults_separate_snapshots_from_source_and_compressed_backups(self):
        app = self.app()
        self.assertEqual(app.autosave_delay, 60)
        self.assertEqual(Path(app.source.backup_directory), self.config / "backup")
        self.assertEqual(app.autosaves().directory.parent, self.config / "autosave")
        original = self.path.read_bytes()
        app.insert_text("Updated ")
        app.last_autosave = app.last_checkpoint = 0
        app.last_edit = 59
        with patch("sprig.ui_storage.time.monotonic", return_value=59):
            app.storage_tick()
        self.assertFalse(app.autosaves().paths())
        with patch("sprig.ui_storage.time.monotonic", return_value=60):
            app.storage_tick()
        snapshots = app.autosaves().paths()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].suffix, ".opml")
        self.assertIn(b"Updated", snapshots[0].read_bytes())
        self.assertEqual(self.path.read_bytes(), original)
        with patch("sprig.ui_storage.time.monotonic", return_value=120):
            app.storage_tick()
        self.assertEqual(app.autosaves().paths(), snapshots)
        app.save()
        manager = Backups(self.path)
        self.assertEqual(manager.read(manager.paths()[-1]), self.path.read_bytes())
        self.assertIn(b"Updated", self.path.read_bytes())

    def test_backup_and_autosave_preferences_persist_and_custom_directories_work(self):
        app = self.app()
        app.set_storage_preference("backup_directory", str(self.directory / "archive"))
        app.set_storage_preference("autosave_directory", ".local-snapshots")
        app.set_storage_preference("autosave_interval", "120")
        app.set_storage_preference("backup_count", "3")
        app.insert_text("Custom ")
        self.assertTrue(app.save())
        self.assertTrue(list((self.directory / "archive").rglob("*.opml.gz")))
        self.assertEqual(app.autosaves().directory.parent, self.directory / ".local-snapshots")
        settings = StateStore().settings
        self.assertEqual(settings["autosave_interval"], 120)
        self.assertEqual(settings["backup_count"], 3)
        for value in ("nan", "inf", "-1", "oops"):
            app.set_storage_preference("autosave_interval", value)
            self.assertEqual(app.autosave_delay, 120)

    def test_relative_config_directory_is_resolved_from_working_directory(self):
        previous = Path.cwd()
        try:
            os.chdir(self.directory)
            with patch.dict(os.environ, {"SPRIG_CONFIG_DIR": "portable-config"}):
                store = StateStore()
                self.assertEqual(store.directory, self.directory / "portable-config")
                manager = Backups(self.directory / "somewhere" / "notes.opml")
                self.assertEqual(manager.directory.parent, self.directory / "portable-config" / "backup")
        finally:
            os.chdir(previous)

    def test_gzip_rotation_checksums_collision_and_distinct_sources(self):
        manager = Backups(self.path, 2)
        paths = [manager.create(f"version {i}".encode()) for i in range(4)]
        self.assertEqual(len(set(paths)), 4)
        self.assertRegex(paths[0].name, r"^notes_\d{4}-\d{2}-\d{2}_\d{6}\.opml\.gz$")
        self.assertEqual(manager.create(b"version 3"), paths[-1])
        manager.prune()
        self.assertEqual(manager.paths(), paths[-2:])
        self.assertEqual(manager.read(paths[-1]), b"version 3")
        other = Backups(self.directory / "other" / "notes.opml", 2)
        self.assertNotEqual(other.directory, manager.directory)
        paths[-1].write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            manager.prune()
        self.assertTrue(paths[-2].exists())

    def test_damaged_or_empty_checksum_is_rejected(self):
        manager = Backups(self.path)
        snapshot = manager.create(b"document")
        snapshot.with_name(snapshot.name + ".sha256").write_text("")
        with self.assertRaises(ValueError):
            manager.read(snapshot)

    def test_cooperative_lock_and_stale_writer_preserve_original(self):
        app = self.app()
        stale, editor = OpmlFile.open(self.path)
        original = self.path.read_bytes()
        app.insert_text("New ")
        with file_lock(self.path):
            with self.assertRaises(FileBusyError):
                app.source.save(app.editor)
        self.assertEqual(self.path.read_bytes(), original)
        app.save()
        editor.change(lambda: editor.insert("Stale "))
        with self.assertRaises(FileChangedError):
            stale.save(editor)
        self.assertIn(b"New", self.path.read_bytes())

    def test_backup_failure_cancels_replacement_and_keeps_recovery(self):
        app = self.app()
        original = self.path.read_bytes()
        app.insert_text("Pending ")
        with patch("sprig.backups.Backups.create", side_effect=OSError("backup unavailable")):
            self.assertFalse(app.save())
        self.assertTrue(app.editor.dirty)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(app.recovery.path.exists())

    def test_overwrite_confirmation_does_not_authorize_later_destination_edits(self):
        app = self.app()
        app.insert_text("Pending ")
        target = self.directory / "other.opml"
        target.write_bytes(self.path.read_bytes())
        app.save_as(str(target))
        external = b'<opml><body><outline text="new external data"/></body></opml>'
        target.write_bytes(external)
        app.handle(Event("text", "y"))
        self.assertEqual(target.read_bytes(), external)
        self.assertEqual(app.source.path, self.path)
        self.assertTrue(app.editor.dirty)
        export = self.directory / "notes.txt"
        export.write_text("before confirmation")
        expected = digest(export.read_bytes())
        export.write_text("changed after confirmation")
        with self.assertRaises(OSError):
            save_export(export, "replace", "txt", overwrite=True, expected=expected)
        self.assertEqual(export.read_text(), "changed after confirmation")

    def test_committed_write_warning_still_updates_identity_and_dirty_state(self):
        app = self.app()
        app.insert_text("Durable? ")
        atomic = app.source._atomic
        def committed(path, *args, **kwargs):
            atomic(path, *args, **kwargs)
            if path == self.path:
                raise CommittedWriteError("simulated directory sync failure")
        with patch.object(app.source, "_atomic", side_effect=committed):
            self.assertTrue(app.save())
        self.assertFalse(app.editor.dirty)
        self.assertEqual(app.source.fingerprint, digest(self.path.read_bytes()))
        self.assertIn("simulated", app.message)
        self.assertTrue(app.save())

    def test_settings_conflict_preserves_other_sessions_preferences(self):
        first, second = StateStore(), StateStore()
        first.settings["search_scope"] = "Global"
        first.save_settings()
        second.settings["search_scope"] = "Current branch"
        with self.assertRaises(OSError):
            second.save_settings()
        self.assertEqual(StateStore().settings["search_scope"], "Global")

    def test_stars_browser_does_not_mask_a_concurrent_view_conflict(self):
        first = self.app()
        second = self.app()
        first.editor.stars.add(first.editor.selected.uid)
        first.persist_view()
        second.star_browser()
        second.persist_view()
        self.assertIn("changed in another session", second.message)
        source, editor = OpmlFile.open(self.path)
        store = StateStore()
        store.bind(source, editor)
        self.assertTrue(editor.stars)

    def test_invalid_settings_schema_and_newer_versions_are_preserved(self):
        self.config.mkdir()
        path = self.config / "settings.json"
        for data in ({"version": 2}, {"backup_count": "bad"}, {"autosave_interval": float("nan")}, {"search_scope": []}):
            content = json.dumps(data)
            path.write_text(content)
            store = StateStore()
            with self.assertRaises(OSError):
                store.save_settings()
            self.assertEqual(path.read_text(), content)

    def test_legacy_metadata_migrates_without_changing_old_files(self):
        app = self.app()
        legacy = self.directory / "outliner-tui"
        old = StateStore(legacy)
        old.bind(app.source, app.editor)
        app.editor.stars.add(app.editor.selected.uid)
        old.save_view(app.source, app.editor)
        old.config_path.unlink()
        (legacy / "settings.json").write_text(json.dumps({"version": 1, "search_scope": "Global"}))
        before = (legacy / "settings.json").read_bytes()
        with patch.dict(os.environ, {"SPRIG_CONFIG_DIR": "", "OUTLINER_CONFIG_DIR": "", "XDG_CONFIG_HOME": str(self.directory)}):
            new = StateStore()
            source, editor = OpmlFile.open(self.path)
            new.bind(source, editor)
            self.assertEqual(new.settings["search_scope"], "Global")
            self.assertTrue(editor.stars)
            new.save_settings()
            new.save_view(source, editor)
            self.assertTrue((self.directory / "sprig-tui" / "config.toml").exists())
        self.assertEqual((legacy / "settings.json").read_bytes(), before)

    def test_recovery_ignores_live_sessions_and_detects_external_edits(self):
        crashed = self.app()
        crashed.insert_text("Unsaved ")
        crashed.editor.stars.add(crashed.editor.selected.uid)
        crashed.checkpoint()
        reopened = self.app()
        self.assertFalse(reopened.recovery.pending()[0])
        crashed.recovery.close()
        records, errors = reopened.recovery.pending()
        self.assertEqual(len(records), 1)
        self.assertFalse(errors)
        external = b'<opml version="2.0"><body><outline text="external edit"/></body></opml>'
        self.path.write_bytes(external)
        reopened.restore_recovery(records[0][0])
        self.assertTrue(reopened.editor.dirty)
        self.assertTrue(reopened.editor.selected.text.startswith("Unsaved"))
        self.assertIn(reopened.editor.selected.uid, reopened.editor.stars)
        self.assertFalse(reopened.save())
        self.assertEqual(self.path.read_bytes(), external)
        self.assertTrue(records[0][0].exists())
        copy = self.directory / "recovered.opml"
        self.assertTrue(reopened.save(str(copy)))
        self.assertIn(b"Unsaved", copy.read_bytes())
        self.assertFalse(records[0][0].exists())

    def test_corrupt_recovery_never_replaces_editor_and_is_preserved(self):
        app = self.app()
        app.insert_text("Keep ")
        app.checkpoint()
        path = app.recovery.path
        data = json.loads(path.read_text())
        data["opml"] += "corrupt"
        path.write_text(json.dumps(data))
        original = app.editor.signature()
        app.restore_recovery(path)
        self.assertEqual(app.editor.signature(), original)
        self.assertTrue(path.exists())

    def test_restore_compressed_snapshot_as_dirty_buffer(self):
        app = self.app()
        manager = Backups(self.path)
        original = manager.paths()[-1]
        app.insert_text("New ")
        app.save()
        app.restore_snapshot(original)
        self.assertTrue(app.editor.dirty)
        self.assertFalse(app.editor.selected.text.startswith("New"))
        self.assertIn(b"New", self.path.read_bytes())

    def test_move_copy_and_back_forward_retain_edits_and_undo(self):
        app = self.app()
        app.handle(Event("f7"))
        app.insert_text("Edited ")
        signature = app.editor.signature()
        app.handle(Event("alt+left"))
        self.assertIs(app.editor.zoom, app.editor.root)
        self.assertEqual(app.editor.signature(), signature)
        app.handle(Event("alt+right"))
        self.assertEqual(app.editor.zoom.text, "Edited First #work @Sky")
        app.editor.home()
        selected, destination = app.editor.root.children
        app.change(lambda: app.editor.transfer(destination))
        self.assertIs(selected.parent, destination)
        app.change(lambda: app.editor.transfer(app.editor.root, copy=True))
        self.assertNotEqual(app.editor.selected.uid, selected.uid)
        self.assertEqual(app.editor.selected.attrs, selected.attrs)
        app.editor.undo()
        self.assertEqual(len(app.editor.root.children), 1)
        app.editor.undo()
        self.assertEqual(len(app.editor.root.children), 2)

    def test_transfer_picker_excludes_descendants_and_finds_destination(self):
        app = self.app()
        app.transfer_picker()
        self.assertEqual([item.title for item in app.picker.matches], ["Home (document root)", "Second"])
        app.handle(Event("text", "Second"))
        app.handle(Event("enter"))
        self.assertEqual(app.editor.root.children[0].children[0].text, "First #work @Sky")

    def test_cancelled_history_file_switch_preserves_history_and_buffer(self):
        app = self.app()
        other = self.directory / "other.opml"
        source, editor = OpmlFile.open(other)
        source.save(editor)
        app.switch_file(str(other))
        app.insert_text("Stay ")
        before = list(app.history.back)
        app.navigate_history()
        self.assertIsNotNone(app.prompt)
        app.handle(Event("escape"))
        self.assertEqual(app.history.back, before)
        self.assertEqual(app.source.path, other)
        self.assertTrue(app.editor.selected.text.startswith("Stay"))

    def test_tree_transaction_rolls_back_partial_edits_and_rejects_cycles(self):
        app = self.app()
        before = app.editor.snapshot()
        def broken():
            app.editor.selected.attrs["text"] = "partial"
            app.editor.root.add(Node({"text": "partial new node"}))
            raise ValueError("injected failure")
        with self.assertRaises(ValueError):
            app.editor.change(broken)
        self.assertEqual(app.editor.snapshot(), before)
        node = app.editor.selected
        node.add(Node({"text": "Child"}))
        with self.assertRaises(ValueError):
            node.children[0].add(node)
        app.editor.validate()
        node.children.append(node)
        with self.assertRaises(ValueError):
            app.source.save(app.editor)

    def test_extensions_retain_order_and_invalid_xml_never_overwrites_source(self):
        content = b'<opml><head/><body><outline text="A"><outline text="B"/><extra id="1"/><outline text="C"/><extra id="2"/></outline></body></opml>'
        self.path.write_bytes(content)
        source, editor = OpmlFile.open(self.path)
        editor.change(lambda: editor.insert("Changed "))
        source.save(editor)
        import xml.etree.ElementTree as ET
        outline = ET.parse(self.path).find("body/outline")
        self.assertEqual([e.tag for e in outline], ["outline", "extra", "outline", "extra"])
        previous = self.path.read_bytes()
        editor.change(lambda: editor.insert("\x00"))
        with self.assertRaises(OpmlError):
            source.save(editor)
        self.assertEqual(self.path.read_bytes(), previous)
        with self.assertRaises(OpmlError):
            OpmlFile.decode(self.path, b'<!DOCTYPE opml [<!ENTITY a "x">]><opml><body/></opml>')

    def test_foreign_outline_elements_remain_extensions(self):
        content = b'<opml xmlns:x="urn:other"><head/><x:body><x:outline>metadata</x:outline></x:body><body><outline text="A"><x:outline>more metadata</x:outline><outline text="B"/></outline></body></opml>'
        source, editor = OpmlFile.decode(self.path, content)
        self.assertEqual([node.text for node in editor.root.walk()], ["", "A", "B"])
        import xml.etree.ElementTree as ET
        output = ET.fromstring(source.encode(editor))
        self.assertEqual(output.find("{urn:other}body/{urn:other}outline").text, "metadata")
        self.assertEqual(output.find("body/outline/{urn:other}outline").text, "more metadata")

    def test_namespaced_opml_remains_namespaced(self):
        content = b'<opml xmlns="urn:opml" version="2.0"><head><title>Namespaced</title></head><body><outline text="A"><outline text="B"/></outline></body></opml>'
        source, editor = OpmlFile.decode(self.path, content)
        self.assertEqual(source.title, "Namespaced")
        import xml.etree.ElementTree as ET
        output = ET.fromstring(source.encode(editor))
        self.assertEqual(len(list(output.iter("{urn:opml}outline"))), 2)

    def test_menus_use_double_borders_and_single_separators(self):
        app = self.app()
        app.handle(Event("escape"))
        app.draw()
        self.assertTrue(any("╔═" in text for text in app.screen.text))
        self.assertTrue(any("╟─" in text for text in app.screen.text))

    def test_invalid_keyboard_codepoints_are_ignored(self):
        self.assertEqual(decode_sequence("[9999999u").key, "unknown")

    def test_incremental_search_and_unicode_highlights_keep_correct_matches(self):
        root = Node(uid="root")
        for value in ("Straße #work", "Mirrors @Sky", "Moonrise"):
            root.add(Node({"text": value}))
        index = Index(root, self.path)
        for query in ("m", "mi", "mir", "m", "#w", "#work", "", "sse"):
            actual = [hit.node for hit in index.search(query)]
            expected = [hit.node for hit in Index(root, self.path).search(query)]
            self.assertEqual(actual, expected)
        self.assertEqual(fuzzy("sse", "Straße")[1], (4, 5))


if __name__ == "__main__":
    unittest.main()
