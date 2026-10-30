from pathlib import Path
import os
import stat
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from sprig.model import Editor, Node
from tests.fixtures import EXAMPLES
from sprig.opml import FileChangedError, OpmlError, OpmlFile


EXAMPLE = EXAMPLES / "demo.opml"


def records(root):
    result = []
    stack = [(e, 0) for e in reversed(root) if e.tag == "outline"]
    while stack:
        element, depth = stack.pop()
        result.append((depth, dict(element.attrib)))
        stack.extend((e, depth + 1) for e in reversed(element) if e.tag == "outline")
    return result


class OpmlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "test.opml"
        environment = patch.dict(os.environ, {"SPRIG_CONFIG_DIR": str(self.path.parent / "config")})
        environment.start()
        self.addCleanup(environment.stop)

    def test_workflowy_fixture_round_trips_all_attributes_and_hierarchy(self):
        original_bytes = EXAMPLE.read_bytes()
        self.path.write_bytes(original_bytes)
        source, e = OpmlFile.open(self.path)
        source.save(e)
        original = ET.fromstring(original_bytes)
        result = ET.parse(self.path).getroot()
        self.assertEqual(records(result.find("body")), records(original.find("body")))
        self.assertEqual(ET.tostring(result.find("head")), ET.tostring(original.find("head")))
        self.assertEqual(len(records(result.find("body"))), 193)
        self.assertEqual(EXAMPLE.read_bytes(), original_bytes)

    def test_unicode_markup_special_characters_multiline_notes_and_completion(self):
        source, e = OpmlFile.open(self.path)
        attrs = {"text": '<b>日本語 🌱 &amp; café</b> &lt;tag&gt; "quoted"',
                 "_note": "one\ntwo\tthree & <four>\n", "_complete": "true", "custom": "don't drop"}
        e.selected.attrs = attrs.copy()
        source.save(e)
        _, result = OpmlFile.open(self.path)
        self.assertEqual(result.selected.attrs, attrs)
        self.assertEqual(result.selected.text, '日本語 🌱 & café <tag> "quoted"')
        self.assertTrue(result.selected.completed)
        self.assertIn(b"&#10;", self.path.read_bytes())

    def test_edit_retains_unknown_metadata_namespaces_comments_and_extensions(self):
        self.path.write_text('''<opml version="2.0" custom="root" xmlns:x="urn:test">
          <head><title>My outline</title><x:meta x:id="1">a &amp; b</x:meta></head>
          <!--keep--><body custom="body"><outline text="hello" custom="node">
            <x:extension>keep <x:child /> this</x:extension><outline text="child" />
          </outline><x:body-extra /></body><x:root-extra /></opml>''')
        source, e = OpmlFile.open(self.path)
        e.change(lambda: e.insert("X"))
        source.save(e)
        tree = ET.parse(self.path).getroot()
        self.assertEqual(tree.get("custom"), "root")
        self.assertEqual(tree.find("body").get("custom"), "body")
        self.assertEqual(tree.find("body/outline").get("custom"), "node")
        self.assertEqual(tree.find("head/{urn:test}meta").get("{urn:test}id"), "1")
        self.assertEqual(tree.find("head/title").text, "My outline")
        self.assertEqual(tree.find("body/outline/{urn:test}extension").text, "keep ")
        self.assertIsNotNone(tree.find("body/{urn:test}body-extra"))
        self.assertIsNotNone(tree.find("{urn:test}root-extra"))
        self.assertIn(b"<!--keep-->", self.path.read_bytes())

    def test_backup_is_previous_version_and_save_clears_dirty(self):
        self.path.write_bytes(EXAMPLE.read_bytes())
        previous = self.path.read_bytes()
        source, e = OpmlFile.open(self.path)
        e.change(lambda: e.insert("updated "))
        self.assertTrue(e.dirty)
        source.save(e)
        self.assertFalse(e.dirty)
        self.assertEqual(self.path.with_suffix(".opml.bak").read_bytes(), previous)
        previous = self.path.read_bytes()
        e.change(lambda: e.insert("again "))
        source.save(e)
        self.assertEqual(self.path.with_suffix(".opml.bak").read_bytes(), previous)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_external_modification_is_not_overwritten(self):
        self.path.write_bytes(EXAMPLE.read_bytes())
        source, e = OpmlFile.open(self.path)
        e.change(lambda: e.insert("mine"))
        external = b'<opml version="2.0"><head/><body><outline text="external"/></body></opml>'
        self.path.write_bytes(external)
        with self.assertRaises(FileChangedError):
            source.save(e)
        self.assertEqual(self.path.read_bytes(), external)
        self.assertTrue(e.dirty)

    def test_external_deletion_and_unexpected_new_file_are_conflicts(self):
        self.path.write_bytes(EXAMPLE.read_bytes())
        source, e = OpmlFile.open(self.path)
        self.path.unlink()
        with self.assertRaises(FileChangedError):
            source.save(e)
        source, e = OpmlFile.open(self.path)
        self.path.write_text("created by another process")
        with self.assertRaises(FileChangedError):
            source.save(e)

    def test_failed_replace_preserves_original_and_unsaved_edits(self):
        self.path.write_bytes(EXAMPLE.read_bytes())
        previous = self.path.read_bytes()
        source, e = OpmlFile.open(self.path)
        e.change(lambda: e.insert("mine"))
        atomic = source._atomic

        def fail_target(path, content, mode, **kwargs):
            if path == self.path:
                raise OSError("disk full")
            atomic(path, content, mode, **kwargs)

        with patch.object(source, "_atomic", side_effect=fail_target):
            with self.assertRaises(OSError):
                source.save(e)
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertTrue(e.dirty)

    def test_external_change_during_save_does_not_overwrite_new_content(self):
        self.path.write_bytes(EXAMPLE.read_bytes())
        source, e = OpmlFile.open(self.path)
        e.change(lambda: e.insert("mine"))
        original_encode = source.encode
        external = b'<opml version="2.0"><head/><body><outline text="external"/></body></opml>'

        def concurrent_change(editor):
            content = original_encode(editor)
            self.path.write_bytes(external)
            return content

        with patch.object(source, "encode", side_effect=concurrent_change):
            with self.assertRaises(FileChangedError):
                source.save(e)
        self.assertEqual(self.path.read_bytes(), external)
        self.assertTrue(e.dirty)
        self.assertEqual(list(self.path.parent.glob(".*.tmp")), [])

    def test_save_as_requires_explicit_overwrite_and_preserves_source(self):
        self.path.write_bytes(EXAMPLE.read_bytes())
        source, e = OpmlFile.open(self.path)
        target = self.path.with_name("other.opml")
        target.write_text("keep me")
        e.change(lambda: e.insert("changed "))
        with self.assertRaises(FileExistsError):
            source.save(e, target)
        self.assertEqual(target.read_text(), "keep me")
        source.save(e, target, overwrite=True)
        self.assertEqual(source.path, target)
        self.assertEqual(target.with_suffix(".opml.bak").read_text(), "keep me")
        self.assertEqual(self.path.read_bytes(), EXAMPLE.read_bytes())

    def test_deep_opml_read_save_and_reload(self):
        root = Node(uid="root")
        parent = root
        for i in range(1800):
            node = Node({"text": f"level {i}", "_note": "a\nb"})
            parent.add(node)
            parent = node
        source = OpmlFile(self.path)
        source.save(Editor(root))
        source, editor = OpmlFile.open(self.path)
        self.assertEqual(len(editor.visible()), 1800)
        source.save(editor)
        _, again = OpmlFile.open(self.path)
        self.assertEqual(len(again.visible()), 1800)
        self.assertEqual(list(again.root.walk())[-1].text, "level 1799")

    def test_empty_body_is_valid_and_new_bullet_is_editable(self):
        self.path.write_text('<opml version="2.0"><head/><body/></opml>')
        source, e = OpmlFile.open(self.path)
        self.assertEqual(e.visible(), [])
        self.assertFalse(e.dirty)
        e.change(lambda: e.insert("first"))
        source.save(e)
        self.assertEqual(ET.parse(self.path).find("body/outline").get("text"), "first")

    def test_malformed_or_non_opml_never_becomes_a_blank_document(self):
        for content in ("<broken", "<html/>", '<opml><head/></opml>',
                        '<opml><body/><body/></opml>', '<opml><body><outline/></body></opml>'):
            with self.subTest(content=content):
                self.path.write_text(content)
                with self.assertRaises(OpmlError):
                    OpmlFile.open(self.path)
                self.assertEqual(self.path.read_text(), content)

    def test_file_modes_are_preserved_and_new_files_are_private(self):
        source, e = OpmlFile.open(self.path)
        self.assertFalse(self.path.exists())
        source.save(e)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.path.chmod(0o640)
        source.save(e)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)


if __name__ == "__main__":
    unittest.main()
