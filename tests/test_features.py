import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from sprig.exports import markdown, render, save_export
from sprig.keys import Event
from sprig.model import Editor, Node
from tests.fixtures import EXAMPLES, full_bytes
from sprig.opml import OpmlFile
from sprig.search import Index, fuzzy, labels
from sprig.state import StateStore
from sprig.trash import Trash
from sprig.ui import App


PROJECT = Path(__file__).resolve().parents[1]


def flatten(root):
    result = []
    stack = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        result.append((depth, dict(node.attrs)))
        stack.extend((n, depth + 1) for n in reversed(node.children))
    return result


class Screen:
    def __init__(self):
        self.text = []

    def timeout(self, value):
        pass

    def getmaxyx(self):
        return 24, 100

    def addstr(self, y, x, text, attr=0):
        self.text.append(text)

    def erase(self):
        self.text.clear()

    def refresh(self):
        pass

    def move(self, y, x):
        pass

    def clear(self):
        self.erase()


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.config = self.directory / "config"
        self.environment = patch.dict(os.environ, {"SPRIG_CONFIG_DIR": str(self.config)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.path = self.directory / "notes.opml"

    def app(self, root=None, autosave=False):
        root = root or Node(uid="root")
        if not root.children:
            root.add(Node())
        source = OpmlFile(self.path)
        editor = Editor(root)
        source.save(editor)
        app = App(Screen(), source, editor, autosave=autosave, storage_options={"save_on_exit": autosave})
        self.addCleanup(app.recovery.close)
        return app

    def press(self, app, key, text=""):
        app.handle(Event(key, text))
        app.draw()

    def test_txt_export_matches_public_fixture_exactly(self):
        source, e = OpmlFile.open(EXAMPLES / "demo.opml")
        self.assertEqual(render(e, e.root, "txt"), (EXAMPLES / "demo.txt").read_text())

    def test_markdown_example_layout_and_notes(self):
        _, e = OpmlFile.open(EXAMPLES / "demo.opml")
        actual = render(e, e.root, "md").splitlines()
        expected = (EXAMPLES / "demo.md").read_text().replace("\ufeff", "").splitlines()
        self.assertEqual(actual, expected)

    def test_markdown_preserves_formatting_links_literal_markup_and_multiline_notes(self):
        self.assertEqual(markdown('<b>bold</b> <i>italic</i> <s>old</s> <a href="https://x.test/a b">link</a>'),
                         '**bold** *italic* ~~old~~ [link](https://x.test/a%20b)')
        self.assertEqual(markdown('&lt;raw&gt; *literal*'), '\\<raw\\> \\*literal\\*')
        root = Node(uid="root")
        node = Node({"text": "Title", "_note": "one\n\ntwo"})
        node.add(Node({"text": "done", "_complete": "true"}))
        root.add(node)
        value = render(Editor(root), root, "md")
        self.assertIn('> one\n> \n> two', value)
        self.assertIn('- [x] done', value)

    def test_export_will_not_overwrite_opml_or_existing_export_without_confirmation(self):
        with self.assertRaises(ValueError):
            save_export(self.path, "bad", "txt")
        target = self.directory / "notes.txt"
        save_export(target, "first", "txt")
        with self.assertRaises(FileExistsError):
            save_export(target, "second", "txt")
        self.assertEqual(target.read_text(), "first")

    def test_fuzzy_search_ranks_contiguous_title_and_searches_notes(self):
        root = Node(uid="root")
        for title, note in (("alphabet", ""), ("a long path below", ""), ("Elsewhere", "alpha in note")):
            root.add(Node({"text": title, "_note": note}))
        hits = Index(root, self.path).search("alph")
        self.assertEqual(hits[0].node.text, "alphabet")
        self.assertTrue(any(h.field == "_note" for h in hits))
        self.assertIsNotNone(fuzzy("alpbt", "alphabet"))
        self.assertIsNone(fuzzy("zxy", "alphabet"))

    def test_labels_exclude_emails_and_url_fragments_and_filter_with_ancestors(self):
        text = "#Work @Alice #日本語 #work/sub foo@example.com https://x.test/#anchor"
        self.assertEqual(labels(text), {"#work", "@alice", "#日本語", "#work/sub"})
        root = Node(uid="root")
        parent = Node({"text": "Project"})
        wanted = Node({"text": "Task #Work", "_note": "ask @Alice", "_complete": "true"})
        parent.add(wanted)
        parent.add(Node({"text": "Unrelated #workout"}))
        root.add(parent)
        e = Editor(root)
        e.collapsed.add(parent.uid)
        e.show_completed = False
        e.filter_labels = {"#work", "@alice"}
        self.assertEqual([n for n, _ in e.visible()], [parent, wanted])
        e.filter_labels.clear()
        self.assertEqual([n for n, _ in e.visible()], [parent])

    def test_esc_menu_is_hidden_until_open_and_navigable(self):
        app = self.app()
        app.draw()
        self.assertFalse(app.menu_open)
        self.press(app, "escape")
        self.assertTrue(app.menu_open)
        self.assertIn(" File ", app.screen.text)
        self.assertIn(" Node ", app.screen.text)
        self.assertIn(" Navigate ", app.screen.text)
        self.press(app, "right")
        self.assertEqual(app.menu_group, 1)
        self.press(app, "escape")
        self.assertFalse(app.menu_open)

    def test_autocomplete_and_filter_picker_do_not_confuse_labels_with_templates(self):
        root = Node(uid="root")
        root.add(Node({"text": "Known #project @Alice #template"}))
        root.add(Node())
        app = self.app(root)
        app.editor.select(root.children[1])
        app.insert_text("#pro")
        self.assertEqual(app.completion, ["#project"])
        self.press(app, "tab")
        self.assertEqual(app.editor.selected.text, "#project ")
        self.assertEqual(len(root.children), 2)
        app.apply_label("#template")
        self.assertEqual([n.text for n, _ in app.editor.visible()], ["Known #project @Alice #template"])

    def test_scopes_branch_file_global_and_preference_persistence(self):
        root = Node(uid="root")
        branch = Node({"text": "Branch"})
        branch.add(Node({"text": "needle one"}))
        root.add(branch)
        root.add(Node({"text": "needle two"}))
        app = self.app(root)
        other = self.directory / "other.opml"
        source, editor = OpmlFile.open(other)
        editor.change(lambda: editor.insert("needle three"))
        source.save(editor)
        (self.directory / "trash.opml").write_bytes(other.read_bytes())
        app.editor.zoom_in(branch)
        app.scope = "Current branch"
        self.assertEqual(len(app.search_items("needle")), 1)
        app.scope = "Whole file"
        self.assertEqual(len(app.search_items("needle")), 2)
        app.open_search()
        self.press(app, "tab")
        self.assertEqual(app.scope, "Global")
        self.assertEqual(len(app.search_items("needle")), 3)
        self.assertEqual(StateStore().settings["search_scope"], "Global")

    def test_star_view_state_round_trip_does_not_change_opml(self):
        root = Node(uid="root")
        parent = Node({"text": "Project"})
        child = Node({"text": "Favourite", "_note": "a note"})
        parent.add(child)
        root.add(parent)
        app = self.app(root)
        before = self.path.read_bytes()
        app.editor.select(child)
        app.toggle_star()
        app.editor.zoom_in(parent)
        app.editor.select(child)
        app.persist_view()
        source, reopened = OpmlFile.open(self.path)
        store = StateStore()
        store.bind(source, reopened)
        self.assertEqual(reopened.zoom.text, "Project")
        self.assertEqual(reopened.selected.text, "Favourite")
        self.assertIn(reopened.selected.uid, reopened.stars)
        self.assertFalse(reopened.dirty)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn(b"star", before)
        self.assertTrue((self.config / "files").exists())

    def test_starred_nodes_can_be_opened_after_closing_file(self):
        app = self.app()
        app.insert_text("Saved star")
        app.toggle_star()
        app.save()
        app.close_file()
        self.assertTrue(app.closed)
        app.star_browser()
        self.assertEqual(len(app.picker.matches), 1)
        self.press(app, "enter")
        self.assertFalse(app.closed)
        self.assertEqual(app.editor.zoom.text, "Saved star")

    def test_corrupt_settings_are_preserved(self):
        self.config.mkdir()
        settings = self.config / "settings.json"
        settings.write_text("broken json")
        store = StateStore()
        with self.assertRaises(OSError):
            store.save_settings()
        self.assertEqual(settings.read_text(), "broken json")

    def test_external_reorder_stars_follow_ancestor_context(self):
        root = Node(uid="root")
        for name in ("One", "Two"):
            parent = Node({"text": name})
            parent.add(Node({"text": "Same"}))
            root.add(parent)
        app = self.app(root)
        app.editor.select(root.children[0].children[0])
        app.toggle_star()
        source, external = OpmlFile.open(self.path)
        external.root.children.reverse()
        source.save(external)
        source, reopened = OpmlFile.open(self.path)
        store = StateStore()
        store.bind(source, reopened)
        favourite = next(n for n in reopened.root.walk() if n.uid in reopened.stars)
        self.assertEqual(favourite.parent.text, "One")
        self.assertFalse(store.unresolved)

    def test_ambiguous_star_is_preserved_and_can_be_removed(self):
        root = Node(uid="root")
        parent = Node({"text": "Parent"})
        parent.add(Node({"text": "Same"}))
        parent.add(Node({"text": "Same"}))
        root.add(parent)
        app = self.app(root)
        app.editor.select(parent.children[0])
        app.toggle_star()
        source, external = OpmlFile.open(self.path)
        external.root.children[0].add(Node({"text": "Same"}))
        source.save(external)
        source, editor = OpmlFile.open(self.path)
        reopened = App(Screen(), source, editor, autosave=False)
        self.addCleanup(reopened.recovery.close)
        self.assertFalse(reopened.editor.stars)
        self.assertEqual(len(reopened.store.unresolved), 1)
        reopened.star_browser()
        self.assertEqual(len(reopened.picker.matches), 1)
        self.press(reopened, "ctrl+d")
        self.assertFalse(reopened.picker.matches)
        self.assertFalse(reopened.store.unresolved)

    def test_recursive_folding_and_single_node_folding_are_distinct(self):
        root = Node(uid="root")
        parent, branch, leaf = Node({"text": "Parent"}), Node({"text": "Branch"}), Node({"text": "Leaf"})
        root.add(parent)
        parent.add(branch)
        branch.add(leaf)
        app = self.app(root)
        app.fold(False, True)
        self.assertEqual([n.text for n, _ in app.editor.visible()], ["Parent"])
        app.fold(True)
        self.assertEqual([n.text for n, _ in app.editor.visible()], ["Parent", "Branch"])
        app.fold(True, True)
        self.assertEqual([n.text for n, _ in app.editor.visible()], ["Parent", "Branch", "Leaf"])
        self.assertFalse(app.editor.dirty)

    def test_editing_label_under_filter_keeps_the_edited_node_until_navigation(self):
        root = Node(uid="root")
        first, second = Node({"text": "First #work"}), Node({"text": "Second #work"})
        root.add(first)
        root.add(second)
        app = self.app(root)
        app.apply_label("#work")
        app.editor.select(first)
        app.change(lambda: app.editor.replace(6, 11, "#home"))
        app.draw()
        self.assertIs(app.editor.selected, first)
        self.assertIn(first, [n for n, _ in app.editor.visible()])
        app.editor.select(second)
        self.assertNotIn(first, [n for n, _ in app.editor.visible()])
        self.assertEqual(first.text, "First #home")

    def test_zoomed_page_can_fold_and_enter_reveals_new_child(self):
        root = Node(uid="root")
        parent = Node({"text": "Page"})
        parent.add(Node({"text": "Child"}))
        root.add(parent)
        app = self.app(root)
        app.editor.zoom_in(parent)
        app.fold(False)
        self.assertEqual([n.text for n, _ in app.editor.visible()], ["Page"])
        app.fold(True)
        self.assertEqual([n.text for n, _ in app.editor.visible()], ["Page", "Child"])
        app.fold(False)
        app.change(app.editor.enter)
        self.assertEqual(len(app.editor.visible()), 3)
        self.assertIs(app.editor.selected, parent.children[0])

    def test_import_replaces_only_empty_leaf_and_is_one_undo_action(self):
        imported = Node({"text": "Import", "_note": "note", "_complete": "true"})
        imported.add(Node({"text": "Nested"}))
        app = self.app()
        e = app.editor
        e.change(lambda: e.insert_trees([imported]))
        self.assertEqual([n.text for n in e.root.children], ["Import"])
        self.assertEqual(e.selected.note, "note")
        self.assertNotEqual(e.selected.uid, imported.uid)
        e.undo()
        self.assertEqual([n.text for n in e.root.children], [""])
        original = e.root.children[0]
        original.add(Node({"text": "keep child"}))
        e.change(lambda: e.insert_trees([imported]))
        self.assertEqual([n.text for n in e.root.children], ["", "Import"])
        self.assertEqual(original.children[0].text, "keep child")

    def test_import_does_not_replace_blank_bullet_with_notes(self):
        app = self.app()
        app.editor.selected.attrs["_note"] = "keep this note"
        app.change(lambda: app.editor.insert_trees([Node({"text": "New"})]))
        self.assertEqual(len(app.editor.root.children), 2)
        self.assertEqual(app.editor.root.children[0].note, "keep this note")

    def test_import_preview_is_read_only_and_cancel_does_not_change_either_file(self):
        app = self.app()
        imported_path = self.directory / "import.opml"
        imported_path.write_bytes((EXAMPLES / "demo.opml").read_bytes())
        before = imported_path.read_bytes()
        app.import_preview(imported_path)
        self.press(app, "text", "do not edit")
        self.assertFalse(app.editor.dirty)
        self.press(app, "escape")
        self.assertIsNone(app.preview)
        self.assertEqual(imported_path.read_bytes(), before)
        self.assertEqual(len(app.editor.root.children), 1)

    def test_delete_requires_confirmation_backs_up_and_restores_with_notes_and_stars(self):
        root = Node(uid="root")
        parent = Node({"text": "Parent"})
        doomed = Node({"text": "Delete me", "_note": "keep note", "_complete": "true"})
        descendant = Node({"text": "Nested", "_note": "nested note"})
        doomed.add(descendant)
        parent.add(doomed)
        parent.add(Node({"text": "Neighbour"}))
        root.add(parent)
        app = self.app(root)
        app.editor.stars.add(descendant.uid)
        app.editor.select(doomed)
        app.request_delete()
        self.press(app, "text", "n")
        self.assertEqual(len(parent.children), 2)
        self.assertFalse((self.directory / "trash.opml").exists())
        app.request_delete()
        self.press(app, "text", "y")
        self.assertEqual([n.text for n in parent.children], ["Neighbour"])
        trash = Trash(self.path)
        record = trash.records()[0]
        self.assertEqual(flatten(record.children[0]), flatten(doomed))
        app.restore_record(trash, record, "original")
        restored = parent.children[0]
        self.assertEqual(flatten(restored), flatten(doomed))
        self.assertIn(restored.children[0].uid, app.editor.stars)
        self.assertEqual(len(trash.records()), 1)
        self.assertIn("_trash_last_restored", trash.records()[0].attrs)
        self.assertNotIn("_trash_source", restored.attrs)

    def test_failed_trash_backup_prevents_deletion(self):
        app = self.app()
        app.insert_text("Do not lose")
        (self.directory / "trash.opml").write_text("not XML")
        app.request_delete()
        self.press(app, "text", "y")
        self.assertEqual(app.editor.selected.text, "Do not lose")
        self.assertIn("Deletion cancelled", app.message)

    def test_restoring_only_root_node_replaces_the_editor_placeholder(self):
        app = self.app()
        app.insert_text("Only root")
        trash = Trash(self.path)
        trash.append(self.path, app.editor, app.editor.selected)
        record = trash.records()[0]
        app.change(app.editor.delete_node)
        app.restore_record(trash, record, "original")
        self.assertEqual([n.text for n in app.editor.root.children], ["Only root"])

    def test_restore_when_original_parent_was_deleted_falls_back_to_root(self):
        root = Node(uid="root")
        parent = Node({"text": "Parent"})
        child = Node({"text": "Child", "_note": "note"})
        parent.add(child)
        root.add(parent)
        app = self.app(root)
        app.editor.select(child)
        trash = Trash(self.path)
        trash.append(self.path, app.editor, child)
        record = trash.records()[0]
        app.editor.select(parent)
        app.change(app.editor.delete_node)
        app.restore_record(trash, record, "original")
        self.assertIn("Child", [n.text for n in app.editor.root.children])
        self.assertIn("Original parent unavailable", app.message)

    def test_close_with_unsaved_changes_can_cancel_or_save_then_open_another_file(self):
        app = self.app()
        app.insert_text("Unsaved")
        app.close_file()
        self.press(app, "escape")
        self.assertFalse(app.closed)
        app.close_file()
        self.press(app, "text", "s")
        self.assertTrue(app.closed)
        self.assertEqual(OpmlFile.open(self.path)[1].selected.text, "Unsaved")
        app.switch_file(str(self.directory / "another.opml"))
        self.assertFalse(app.closed)
        self.assertEqual(app.source.path.name, "another.opml")

    def test_large_document_round_trip_and_search_cover_every_node_and_attribute(self):
        self.path.write_bytes(full_bytes())
        source, editor = OpmlFile.open(self.path)
        original = ET.parse(source.path).getroot()
        encoded = ET.fromstring(source.encode(editor))
        before = [(n.attrib, len(n)) for n in original.iter("outline")]
        after = [(n.attrib, len(n)) for n in encoded.iter("outline")]
        self.assertEqual(len(before), 8852)
        self.assertEqual(before, after)
        self.assertEqual(sum("_note" in n for n, _ in after), 915)
        self.assertEqual(sum(n.get("_complete") == "true" for n, _ in after), 32)
        index = Index(editor.root, source.path)
        self.assertEqual(len(index.entries), 8852)
        for kind in ("md", "txt"):
            value = render(editor, editor.root, kind)
            self.assertGreater(len(value), 100_000)
            self.assertNotIn("<time ", value)


if __name__ == "__main__":
    unittest.main()
