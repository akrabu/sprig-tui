"""Exercise the actual curses app in a pseudo-terminal, not a mocked screen."""

import fcntl
import gc
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from sprig.model import Editor, Node
from tests.fixtures import EXAMPLES, full_bytes
from sprig.opml import OpmlFile
from sprig.preferences import encode
from sprig.state import StateStore


PROJECT = Path(__file__).resolve().parents[1]


class Terminal:
    def __init__(self, path, *options, config_directory=None):
        # Parent/child links form cycles. Release old fixture trees before
        # launching another interpreter on machines with little free memory.
        gc.collect()
        self.master, slave = os.openpty()
        self.output = bytearray()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 90, 0, 0))
        environment = dict(os.environ, TERM="xterm-256color",
                           LC_ALL="en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8",
                           PYTHONDONTWRITEBYTECODE="1")
        environment["SPRIG_CONFIG_DIR"] = str(config_directory or Path(path).parent / ".sprig-test-config")
        try:
            self.process = subprocess.Popen(
                [sys.executable, *(["-S"] if sys.flags.no_site else []), "-B", str(PROJECT / "sprig.py"), *options, str(path)],
                stdin=slave, stdout=slave, stderr=slave, cwd=PROJECT, env=environment,
            )
        finally:
            os.close(slave)
        try:
            self.wait_for(lambda: b"SPRIG" in self.output and b"^Q quit" in self.output, timeout=30)
        except BaseException:
            self.close()
            raise

    def drain(self, duration=0.12):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                return
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                return
            if not chunk:
                return
            self.output.extend(chunk)

    def wait_for(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain(0.05)
            if predicate():
                return
            if self.process.poll() is not None:
                if predicate():
                    return
                break
            # A closed PTY is immediately readable forever. Yield while the
            # child finishes interpreter shutdown instead of polling at full CPU.
            time.sleep(0.005)
        if predicate():
            return
        raise AssertionError("Terminal condition timed out:\n" + self.output[-6000:].decode("utf-8", "replace"))

    def send(self, text):
        os.write(self.master, text.encode("utf-8") if isinstance(text, str) else text)
        self.drain()

    def resize(self, rows, columns):
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        os.kill(self.process.pid, signal.SIGWINCH)
        self.drain()

    def finish(self):
        self.send(b"\x11")  # Ctrl+Q
        self.wait_for(lambda: self.process.poll() is not None, timeout=45)
        self.drain(0.02)
        if self.process.returncode != 0 or b"Traceback" in self.output:
            raise AssertionError(self.output[-6000:].decode("utf-8", "replace"))

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(2)
        os.close(self.master)


class TerminalWaitTests(unittest.TestCase):
    def test_exit_between_poll_calls_satisfies_wait(self):
        terminal = object.__new__(Terminal)
        terminal.output = bytearray()
        terminal.drain = lambda duration: None
        terminal.process = Mock()
        terminal.process.poll.side_effect = [None, 0, 0]
        terminal.wait_for(lambda: terminal.process.poll() is not None)


@unittest.skipUnless(os.name == "posix", "requires a POSIX terminal")
class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name).resolve() / "outline.opml"
        environment = patch.dict(os.environ, {"SPRIG_CONFIG_DIR": str(self.path.parent / ".sprig-test-config")})
        environment.start()
        self.addCleanup(environment.stop)

    def terminal(self, *options, path=None):
        terminal = Terminal(self.path if path is None else path, *options,
                            config_directory=self.path.parent / ".sprig-test-config")
        self.addCleanup(terminal.close)
        return terminal

    def test_edit_indent_note_complete_zoom_and_save(self):
        t = self.terminal("--no-autosave", "--no-save-on-exit")
        t.send("Projects\rAlpha\t")
        t.send("\x1b[17~")  # F6: note
        t.send("note & <raw>\nsecond line")
        t.send("\x1b[17~")
        t.send("\x1b[15~")  # F5: complete
        t.send("\x1b[13;5u\x1b[13;5u")  # Ctrl+Enter through CSI-u
        t.send("\x1b.\rDetail\x1b.\x1b,\x1b,\x1b,")
        t.send(b"\x13")  # Ctrl+S
        t.finish()
        _, e = OpmlFile.open(self.path)
        self.assertEqual([n.text for n in e.root.children], ["Projects"])
        alpha = e.root.children[0].children[0]
        self.assertEqual(alpha.text, "Alpha")
        self.assertEqual(alpha.note, "note & <raw>\nsecond line")
        self.assertTrue(alpha.completed)
        self.assertEqual([n.text for n in alpha.children], ["Detail"])

    def test_deep_zoom_edit_preserves_the_whole_document(self):
        root = Node(uid="root")
        parent = root
        for level in range(1600):
            node = Node({"text": f"Level {level}"})
            parent.add(node)
            parent = node
        OpmlFile(self.path).save(Editor(root))
        t = self.terminal("--no-autosave", "--no-save-on-exit")
        t.send("\x06Level 1599\r")  # Search and reveal the deepest item
        t.send("\x1b.\x1b[F!\x13")  # Zoom, End, edit the page title, save
        t.send("\x1b,\x1bOP")  # Zoom out, then open help
        t.finish()  # Quit also works directly from the help panel
        _, e = OpmlFile.open(self.path)
        nodes = list(e.root.walk())[1:]
        self.assertEqual(len(nodes), 1600)
        self.assertEqual(nodes[-1].text, "Level 1599!")

    def test_autosave_and_navigation_without_rewriting(self):
        t = self.terminal("--autosave-interval", "0.2")
        t.send("日本語 🌱 café")
        snapshots = self.path.parent / ".sprig-test-config" / "autosave"
        t.wait_for(lambda: any(snapshots.rglob("*.opml")))
        self.assertFalse(self.path.exists())
        snapshot = next(snapshots.rglob("*.opml"))
        before = snapshot.read_bytes()
        mtime = snapshot.stat().st_mtime_ns
        t.send("\x1b.\x1b,\x1b[1;5A\x1b[1;5B")
        t.finish()
        self.assertEqual(snapshot.read_bytes(), before)
        self.assertEqual(snapshot.stat().st_mtime_ns, mtime)
        _, e = OpmlFile.open(self.path)
        self.assertEqual(e.selected.text, "日本語 🌱 café")
        self.assertTrue(any((snapshots.parent / "backup").rglob("*.opml.gz")))

    def test_killed_process_can_restore_unsaved_buffer_in_app(self):
        t = self.terminal("--no-save-on-exit")
        t.send("Recovered after crash 🌱")
        recovery = self.path.parent / ".sprig-test-config" / "recovery"
        t.wait_for(lambda: any(recovery.glob("*.json")))
        t.process.kill()
        t.process.wait(5)
        self.assertFalse(self.path.exists())
        reopened = self.terminal("--no-autosave", "--no-save-on-exit")
        reopened.send("y\ry\x13")  # Browse recovery, choose, confirm, save.
        reopened.finish()
        self.assertEqual(OpmlFile.open(self.path)[1].selected.text, "Recovered after crash 🌱")
        self.assertFalse(list(recovery.glob("*.json")))

    def test_punctuation_folding_and_f9_preserve_opml(self):
        original = b'<opml><body><outline text="Parent"><outline text="Child"/></outline></body></opml>'
        self.path.write_bytes(original)
        t = self.terminal("--no-autosave", "--no-save-on-exit")
        for collapse, expand in (("\x1b[44;5u", "\x1b[46;5u"), ("\x1b[44;6u", "\x1b[46;6u")):
            mark = len(t.output)
            t.send(collapse)
            t.wait_for(lambda: "▸".encode() in t.output[mark:])
            mark = len(t.output)
            t.send(expand)
            t.wait_for(lambda: "▾".encode() in t.output[mark:])
        t.send("\x1b[20~")  # F9
        t.finish()
        source, editor = OpmlFile.open(self.path)
        store = StateStore(self.path.parent / ".sprig-test-config")
        store.bind(source, editor)
        self.assertEqual(len(editor.visible()), 1)
        self.assertEqual(self.path.read_bytes(), original)

    def test_custom_toml_bindings_and_debug_log_in_real_terminal(self):
        self.path.write_text('<opml><body><outline text="Parent"><outline text="Child"/></outline></body></opml>')
        directory = self.path.parent / ".sprig-test-config"
        store = StateStore(directory)
        settings = {**store.settings, "keymap": {"collapse": ["alt+c"], "expand": ["alt+e"]},
                    "debug_enabled": True, "debug_path": "logs/terminal.log"}
        store.config_path.write_bytes(encode(settings))
        t = self.terminal("--no-autosave", "--no-save-on-exit")
        mark = len(t.output)
        t.send("\x1bc")
        t.wait_for(lambda: "▸".encode() in t.output[mark:])
        mark = len(t.output)
        t.send("\x1be")
        t.wait_for(lambda: "▾".encode() in t.output[mark:])
        t.send("PRIVATE_TYPED_TEXT\x13")
        t.finish()
        log = (directory / "logs" / "terminal.log").read_text()
        self.assertIn('"command": "collapse"', log)
        self.assertIn('"key": "alt+e"', log)
        self.assertNotIn("PRIVATE_TYPED_TEXT", log)

    def test_bracketed_paste_undo_redo_and_resize(self):
        t = self.terminal("--no-autosave", "--no-save-on-exit")
        t.send("\x1b[200~Root\n\tChild\n\t\tGrandchild\nOther\n\x1b[201~")
        t.send(b"\x1a\x19")  # Ctrl+Z, Ctrl+Y
        t.resize(13, 36)
        t.send("\x1b[1;5H\x1b[1;5F")
        t.resize(24, 90)
        t.send(b"\x13")
        t.finish()
        _, e = OpmlFile.open(self.path)
        self.assertEqual([n.text for n in e.root.children], ["Root", "Other"])
        child = e.root.children[0].children[0]
        self.assertEqual(child.text, "Child")
        self.assertEqual(child.children[0].text, "Grandchild")
        self.assertNotIn(b"[201~", self.path.read_bytes())

    def test_sample_search_help_and_zoom_do_not_change_file(self):
        original = (EXAMPLES / "demo.opml").read_bytes()
        self.path.write_bytes(original)
        t = self.terminal("--no-autosave", "--no-save-on-exit")
        t.send("\x1b.\x1b[1;5B\x1b,")
        t.send("\x1bOP")  # F1
        t.send("\x1b[6~")  # Page Down in help
        t.send("\x1b[27u")  # Unambiguous Escape in the negotiated keyboard protocol
        t.send(b"\x06")  # Ctrl+F
        t.send("mirror\r")
        t.send("\x1b.\x1b,\x1b[17~")
        t.send("\x1b[27u")
        t.finish()
        self.assertEqual(self.path.read_bytes(), original)

    def test_save_error_keeps_app_open_and_allows_discard(self):
        target = self.path.parent / "missing" / "outline.opml"
        t = self.terminal("--no-autosave", "--no-save-on-exit", path=target)
        t.send("unsaved text\x13")
        t.wait_for(lambda: b"Save failed" in t.output)
        self.assertIsNone(t.process.poll())
        t.send(b"\x11")
        t.wait_for(lambda: b"Unsaved changes" in t.output)
        t.send("d")
        t.wait_for(lambda: t.process.poll() is not None)
        self.assertEqual(t.process.returncode, 0)
        self.assertFalse(target.exists())

    def test_open_same_file_after_saving_keeps_latest_edits(self):
        t = self.terminal()
        t.send("latest text")
        t.send("\x1bOR")  # F3: open file
        t.send("\x15")  # Ctrl+U: clear path
        t.send(str(self.path) + "\r")
        t.drain(0.3)
        t.send("\x1b[F!")  # End, then insert in reopened latest text
        t.send(b"\x13")
        t.finish()
        _, e = OpmlFile.open(self.path)
        self.assertEqual(e.selected.text, "latest text!")

    def test_menu_import_entire_opml_and_export_both_formats(self):
        imported = self.path.parent / "import-source.opml"
        original = (EXAMPLES / "demo.opml").read_bytes()
        imported.write_bytes(original)
        t = self.terminal("--no-autosave", "--no-save-on-exit")
        t.send("\x1b[27u" + "\x1b[B" * 5 + "\r")  # File → Import
        t.send("\x0c\x15" + str(imported) + "\r")  # Path prompt
        t.wait_for(lambda: b"IMPORT PREVIEW" in t.output)
        t.send("\ry\x13")  # Entire file; confirm; save
        t.send("\x1b[27u\x1b[H" + "\x1b[B" * 6 + "\r\r\r")  # Export TXT / selected / filename
        t.send("\x1b[27u\x1b[H" + "\x1b[B" * 7 + "\r\r\r")  # Export Markdown
        t.finish()
        self.assertEqual(self.path.with_suffix(".txt").read_bytes(), (EXAMPLES / "demo.txt").read_bytes())
        self.assertIn("> Clear skies", self.path.with_suffix(".md").read_text())
        self.assertEqual(imported.read_bytes(), original)
        actual = ET.parse(self.path).getroot()
        wanted = ET.fromstring(original)
        self.assertEqual([n.attrib for n in actual.iter("outline")], [n.attrib for n in wanted.iter("outline")])

    def test_import_search_selects_specific_subtree_and_undo_restores_placeholder(self):
        imported = self.path.parent / "source.opml"
        imported.write_bytes((EXAMPLES / "demo.opml").read_bytes())
        t = self.terminal("--no-autosave", "--no-save-on-exit")
        t.send("\x1b[27u" + "\x1b[B" * 5 + "\r")
        t.send("\x0c\x15" + str(imported) + "\r")
        t.wait_for(lambda: b"IMPORT PREVIEW" in t.output)
        t.send("\x06Mercury\r\ry\x1a\x19\x13")  # Find node; import; undo / redo / save
        t.finish()
        _, e = OpmlFile.open(self.path)
        self.assertEqual([n.text for n in e.root.children], ["Mercury"])
        self.assertEqual([n.text for n in e.root.children[0].children], ["Tasks", "Log"])
        self.assertTrue(any(n.note == "Clear skies" for n in e.root.walk()))

    def test_delete_confirmation_and_restore_through_menu(self):
        t = self.terminal("--no-autosave", "--no-save-on-exit")
        t.send("Keep\rDelete\x1b[17~saved note\x1b[17~\rLeaf\t")
        t.send("\x06Delete\r\x1b[15~\x13")  # Select parent, complete, save
        t.send("\x1b[21~n\x1b[21~y\x13")  # F10 / cancel; F10 / confirm; save
        t.send("\x1b[27u\x1b[C\x1b[C" + "\x1b[B" * 4 + "\r\r\r")
        t.send("\x13")
        t.finish()
        _, e = OpmlFile.open(self.path)
        self.assertEqual([n.text for n in e.root.children], ["Keep", "Delete"])
        restored = e.root.children[1]
        self.assertEqual(restored.note, "saved note")
        self.assertTrue(restored.completed)
        self.assertEqual([n.text for n in restored.children], ["Leaf"])
        records = ET.parse(self.path.parent / "trash.opml").findall("body/outline")
        self.assertEqual(len(records), 1)
        self.assertIn("_trash_last_restored", records[0].attrib)

    def test_close_starred_file_and_reopen_with_stars_hotkey(self):
        t = self.terminal("--no-autosave", "--no-save-on-exit")
        t.send("Saved #work @Alice\x1bs\x13\x17")  # Star / save / close
        t.send("\x1b[23~\r\x1b[17~kept\x13")  # F11 stars, select, edit note, save
        t.finish()
        _, e = OpmlFile.open(self.path)
        self.assertEqual(e.selected.text, "Saved #work @Alice")
        self.assertEqual(e.selected.note, "kept")
        self.assertEqual(set(e.selected.attrs), {"text", "_note"})
        config = self.path.parent / ".sprig-test-config" / "files"
        import json
        data = json.loads(next(config.glob("*.json")).read_text())
        self.assertEqual(len(data["stars"]), 1)

    def test_large_document_edit_undo_save_preserves_every_attribute(self):
        original = full_bytes()
        self.path.write_bytes(original)
        t = self.terminal("--no-autosave", "--no-save-on-exit")
        t.send("\x1b[1;7A")  # Collapse all below current node
        t.send("\x06__zzx_no_match_123__\x1b[27u")
        mark = len(t.output)
        t.send("\x1b[F!\x1a\x13")  # Edit title, undo, save
        # Wait for the queued edit/undo/save before timing application shutdown.
        # On slow storage, recovery checkpoints can delay those operations.
        t.wait_for(lambda: b"Saved outline.opml" in t.output[mark:], timeout=120)
        t.finish()
        before = ET.fromstring(original)
        after = ET.parse(self.path).getroot()
        self.assertEqual([n.attrib for n in before.iter("outline")], [n.attrib for n in after.iter("outline")])
        self.assertEqual(len(list(after.iter("outline"))), 8852)
        self.assertEqual(full_bytes(), original)


if __name__ == "__main__":
    unittest.main()
