import unittest

from sprig.keys import decode_sequence
from sprig.layout import width, wrap
from sprig.richtext import plain, replace, split


class RichTextTests(unittest.TestCase):
    def test_edits_preserve_visible_text_and_surrounding_markup(self):
        for raw in ("abc", "<b>hi</b> &amp; <i>world</i>", "🌱 café &lt;3", "a<br>b", "&NotEqualTilde;x"):
            value = plain(raw)
            for start in range(len(value) + 1):
                for end in range(start, len(value) + 1):
                    with self.subTest(raw=raw, start=start, end=end):
                        result = replace(raw, start, end, "<&X>")
                        self.assertEqual(plain(result), value[:start] + "<&X>" + value[end:])
                        if "<b>" in raw:
                            self.assertIn("<b>", result)
                            self.assertIn("</b>", result)

    def test_split_and_noop_retain_markup(self):
        source = "<b>hello &amp; world</b>"
        self.assertEqual(replace(source, 2, 2, ""), source)
        left, right = split(source, 5)
        self.assertEqual(plain(left), "hello")
        self.assertEqual(plain(right), " & world")
        self.assertTrue(left.startswith("<b>"))
        self.assertTrue(right.endswith("</b>"))

    def test_wrapping_preserves_offsets_for_unicode_spaces_and_newlines(self):
        for text in ("", "long words and spaces  ", "日本語の文🌱 café", "a\n\nb\n", "\ttext", "e\u0301" * 20):
            for cells in (4, 7, 20):
                with self.subTest(text=text, cells=cells):
                    segments = wrap(text, cells)
                    joined = "".join(text[s.start:s.end] for s in segments)
                    self.assertEqual(joined, text.replace("\n", ""))
                    self.assertTrue(all(width(text[s.start:s.end]) <= cells for s in segments))
                    self.assertEqual(segments[-1].end, len(text))

    def test_exact_width_gets_an_extra_cursor_cell(self):
        self.assertEqual([(s.start, s.end) for s in wrap("abcd", 4)], [(0, 4), (4, 4)])

    def test_terminal_modifier_encodings(self):
        cases = {"[1;5A": "ctrl+up", "[1;5B": "ctrl+down", "[1;4A": "alt+shift+up",
                 "[1;3D": "alt+left", "[1;6B": "ctrl+shift+down", "[Z": "shift+tab",
                 "[13;5u": "ctrl+enter", "[13;2u": "shift+enter", "[27;5;13~": "ctrl+enter",
                 "[127;6u": "ctrl+shift+backspace", "[15~": "f5", "[17~": "f6"}
        for raw, key in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(decode_sequence(raw).key, key)
        self.assertEqual(decode_sequence("[97u").text, "a")
        self.assertEqual(decode_sequence("[97;2u").text, "A")
        self.assertEqual(decode_sequence("[32u").text, " ")


if __name__ == "__main__":
    unittest.main()
