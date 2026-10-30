import unittest

from sprig.model import Editor, Node


def outline():
    root = Node(uid="root")
    a, b, c = [Node({"text": text}) for text in ("A", "B", "C")]
    for n in (a, b, c):
        root.add(n)
    a.add(Node({"text": "A1"}))
    a.add(Node({"text": "A2"}))
    b.add(Node({"text": "B1", "_note": "a note"}))
    return Editor(root), a, b, c


def labels(node):
    return [n.text for n in node.children]


class TreeTests(unittest.TestCase):
    def test_indent_moves_entire_branch_and_undo_restores_it(self):
        e, a, b, c = outline()
        e.select(b, 1)
        e.collapsed.add(a.uid)
        e.change(e.indent)
        self.assertEqual(labels(e.root), ["A", "C"])
        self.assertEqual(labels(a), ["A1", "A2", "B"])
        self.assertEqual(b.children[0].note, "a note")
        self.assertNotIn(a.uid, e.collapsed)
        self.assertTrue(e.undo())
        self.assertEqual(labels(e.root), ["A", "B", "C"])
        self.assertEqual(e.selected.text, "B")
        self.assertEqual(e.cursor, 1)
        self.assertTrue(e.redo())
        self.assertEqual(labels(e.root), ["A", "C"])

    def test_outdent_retains_children_and_places_item_after_parent(self):
        e, a, b, c = outline()
        e.select(b)
        e.change(e.indent)
        e.change(e.outdent)
        self.assertEqual(labels(e.root), ["A", "B", "C"])
        self.assertEqual(labels(b), ["B1"])
        self.assertIs(b.children[0].parent, b)

    def test_zoom_boundary_prevents_moving_item_out_of_view(self):
        e, a, b, c = outline()
        e.zoom_in(a)
        e.select(a.children[0])
        self.assertFalse(e.change(e.outdent))
        e.select(a)
        self.assertFalse(e.change(e.indent))
        self.assertFalse(e.change(lambda: e.move(1)))
        self.assertFalse(e.dirty)

    def test_first_sibling_cannot_indent(self):
        e, a, b, c = outline()
        self.assertFalse(e.change(e.indent))
        self.assertEqual(len(e.undo_stack), 0)

    def test_zoom_is_reversible_and_does_not_change_document(self):
        e, a, b, c = outline()
        e.zoom_in(a)
        e.zoom_in(a.children[1])
        self.assertEqual([(n.text, d) for n, d in e.visible()], [("A2", -1)])
        e.zoom_out()
        self.assertIs(e.zoom, a)
        self.assertEqual(e.selected.text, "A2")
        e.zoom_out()
        self.assertIs(e.zoom, e.root)
        self.assertIs(e.selected, a)
        self.assertFalse(e.dirty)

    def test_enter_on_zoomed_leaf_creates_child(self):
        e, a, b, c = outline()
        e.zoom_in(c)
        e.change(e.enter)
        e.change(lambda: e.insert("child"))
        self.assertEqual(labels(c), ["child"])
        self.assertIs(e.zoom, c)

    def test_enter_splits_formatted_text_keeps_notes_and_children(self):
        e, a, b, c = outline()
        a.attrs.update({"text": "<b>hello world</b>", "_note": "keep me"})
        e.select(a, 5)
        e.change(e.enter)
        self.assertEqual(labels(e.root), ["hello", " world", "B", "C"])
        self.assertEqual(a.note, "keep me")
        self.assertEqual(labels(a), ["A1", "A2"])
        self.assertIn("<b>", e.selected.attrs["text"])

    def test_enter_at_start_inserts_empty_item_above(self):
        e, a, b, c = outline()
        e.select(b, 0)
        e.change(e.enter)
        self.assertEqual(labels(e.root), ["A", "", "B", "C"])
        self.assertIs(e.selected, b)

    def test_enter_empty_nested_item_outdents(self):
        e, a, b, c = outline()
        empty = Node()
        a.add(empty)
        e.select(empty)
        e.change(e.enter)
        self.assertIs(empty.parent, e.root)
        self.assertEqual(labels(e.root), ["A", "", "B", "C"])

    def test_move_branch_preserves_descendants_and_selection(self):
        e, a, b, c = outline()
        e.select(b)
        e.change(lambda: e.move(-1))
        self.assertEqual(labels(e.root), ["B", "A", "C"])
        self.assertEqual(labels(b), ["B1"])
        self.assertIs(e.selected, b)

    def test_merge_keeps_both_notes_and_subtrees(self):
        e, a, b, c = outline()
        a.attrs["_note"], b.attrs["_note"] = "first", "second"
        e.select(b, 0)
        e.change(e.backspace)
        self.assertEqual(a.text, "AB")
        self.assertEqual(a.note, "first\nsecond")
        self.assertEqual(labels(a), ["A1", "A2", "B1"])
        self.assertEqual(e.cursor, 1)
        self.assertTrue(all(n.parent is a for n in a.children))
        e.undo()
        self.assertEqual(labels(e.root), ["A", "B", "C"])

    def test_merge_does_not_discard_unknown_metadata(self):
        e, a, b, c = outline()
        b.attrs["custom"] = "keep me"
        e.select(b, 0)
        self.assertFalse(e.change(e.backspace))
        self.assertEqual(b.attrs["custom"], "keep me")

    def test_delete_zoomed_branch_then_undo_restores_zoom_and_notes(self):
        e, a, b, c = outline()
        e.zoom_in(b)
        uid = b.uid
        e.change(e.delete_node)
        self.assertEqual(labels(e.root), ["A", "C"])
        e.undo()
        self.assertEqual(e.zoom.uid, uid)
        self.assertEqual(e.zoom.children[0].note, "a note")

    def test_deleting_last_item_leaves_editable_blank(self):
        root = Node(uid="root")
        root.add(Node({"text": "only"}))
        e = Editor(root)
        e.change(e.delete_node)
        self.assertEqual(labels(e.root), [""])
        self.assertIs(e.selected, e.root.children[0])
        self.assertFalse(e.change(e.backspace))

    def test_duplicate_copies_whole_branch_with_new_identities(self):
        e, a, b, c = outline()
        e.select(b)
        e.change(e.duplicate)
        self.assertEqual(labels(e.root), ["A", "B", "B", "C"])
        clone = e.selected
        self.assertNotEqual(clone.uid, b.uid)
        self.assertNotEqual(clone.children[0].uid, b.children[0].uid)
        self.assertEqual(clone.children[0].attrs, b.children[0].attrs)
        clone.children[0].attrs["_note"] = "different"
        self.assertEqual(b.children[0].note, "a note")

    def test_paste_indentation_and_single_undo(self):
        root = Node(uid="root")
        root.add(Node())
        e = Editor(root)
        e.change(lambda: e.paste_outline("Projects\n\t- Alpha\n\t\tDetail\nOther\n"))
        self.assertEqual(labels(root), ["Projects", "Other"])
        self.assertEqual(labels(root.children[0]), ["Alpha"])
        self.assertEqual(labels(root.children[0].children[0]), ["Detail"])
        self.assertEqual(e.selected.text, "Other")
        e.undo()
        self.assertEqual(labels(e.root), [""])
        e.redo()
        self.assertEqual(labels(e.root), ["Projects", "Other"])

    def test_note_newlines_are_not_new_bullets(self):
        e, a, b, c = outline()
        e.select(a, field="_note")
        e.change(lambda: e.insert("one"))
        e.change(e.enter)
        e.change(lambda: e.insert("two"))
        self.assertEqual(a.note, "one\ntwo")
        self.assertEqual(labels(e.root), ["A", "B", "C"])

    def test_typing_group_and_dirty_state_across_save_undo_redo(self):
        e, a, b, c = outline()
        e.select(c, 1)
        e.change(lambda: e.insert("1"), "typing")
        e.change(lambda: e.insert("2"), "typing")
        self.assertEqual(len(e.undo_stack), 1)
        e.mark_saved()
        self.assertFalse(e.dirty)
        e.undo()
        self.assertTrue(e.dirty)
        self.assertEqual(e.selected.text, "C")
        e.redo()
        self.assertFalse(e.dirty)
        self.assertEqual(e.selected.text, "C12")

    def test_reveal_match_in_completed_collapsed_ancestor(self):
        e, a, b, c = outline()
        a.attrs["_complete"] = "true"
        target = a.children[0]
        target.attrs["_note"] = "Needle in a note"
        e.show_completed = False
        e.collapsed.add(a.uid)
        e.zoom_in(b)
        self.assertEqual(e.search("needle note"), [target])
        e.reveal(target)
        self.assertTrue(e.show_completed)
        self.assertIn(target, [n for n, d in e.visible()])

    def test_deep_nesting_has_no_fixed_depth_limit(self):
        root = Node(uid="root")
        parent = root
        for i in range(1600):
            child = Node({"text": str(i)})
            parent.add(child)
            parent = child
        e = Editor(root)
        self.assertEqual(len(e.visible()), 1600)
        e.zoom_in(parent)
        e.change(lambda: e.insert("x"))
        e.undo()
        self.assertEqual(e.selected.text, "1599")
        for _ in range(1600):
            e.zoom_out()
        self.assertIs(e.zoom, e.root)


if __name__ == "__main__":
    unittest.main()
