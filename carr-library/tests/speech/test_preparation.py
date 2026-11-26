"""Source preservation and auditable pronunciation derivatives; no native speech claim."""
from hashlib import sha256
import unittest

from carr_library.speech.preparation import (
    MAX_READING_CHARACTERS, NarrationDerivative, Pronunciation, ReadingEdit,
    apply_reading_edits, prepare_reading,
)


class ReadingDerivativeTests(unittest.TestCase):
    def test_verbatim_keeps_unicode_and_exact_offsets(self):
        source = "Оригінал e\u0301. Žltý kôň.\r\n[1]"
        result = prepare_reading(source)
        self.assertEqual(result.original_text, source)
        self.assertEqual(result.text, source)
        self.assertEqual(result.source_sha256, result.derivative_sha256)
        self.assertEqual(result.source_span(3, 13), (3, 13))
        self.assertFalse(result.spans[0].changed)

    def test_pronunciation_preserves_original_and_maps_replacement_conservatively(self):
        source = "Read NATO and Dr. Novák."
        result = prepare_reading(source, [Pronunciation("NATO", "Nay toe"),
                                          Pronunciation("Dr.", "Doctor")])
        self.assertEqual(result.text, "Read Nay toe and Doctor Novák.")
        self.assertEqual(result.original_text, source)
        self.assertEqual(result.source_span(5, 8), (5, 9))
        self.assertEqual(result.source_span(17, 20), (14, 17))
        self.assertEqual(result.source_sha256, sha256(source.encode()).hexdigest())
        self.assertNotEqual(result.source_sha256, result.derivative_sha256)

    def test_edits_do_not_cascade(self):
        result = prepare_reading("ABC", [Pronunciation("ABC", "DEF"), Pronunciation("DEF", "wrong")])
        self.assertEqual(result.text, "DEF")
        self.assertEqual(len(result.edits), 1)

    def test_word_boundary_respects_combining_marks(self):
        result = prepare_reading("x e\u0301 x ex", [Pronunciation("e", "eee")])
        self.assertEqual(result.text, "x e\u0301 x ex")

    def test_longest_literal_rule_wins(self):
        result = prepare_reading("New York and New", [Pronunciation("New", "N"),
                                                       Pronunciation("New York", "NY")])
        self.assertEqual(result.text, "NY and N")

    def test_longest_ineligible_whole_word_does_not_hide_valid_shorter_rule(self):
        source = "New Yorkshire and New York"
        result = prepare_reading(source, [Pronunciation("New York", "NYC"),
                                          Pronunciation("New", "Nju")])
        self.assertEqual(result.text, "Nju Yorkshire and NYC")
        self.assertEqual(result.original_text, source)
        self.assertEqual(result.source_span(0, 3), (0, 3))
        self.assertEqual(len(result.edits), 2)

    def test_rejected_literal_does_not_consume_embedded_explicit_substring_rule(self):
        result = prepare_reading("xABCy", [Pronunciation("ABC", "whole word"),
                                            Pronunciation("B", "bee", whole_word=False)])
        self.assertEqual(result.text, "xAbeeCy")
        self.assertEqual(result.source_span(2, 5), (2, 3))

    def test_eligible_shorter_rule_still_respects_combining_boundaries(self):
        source = "New e\u0301x"
        result = prepare_reading(source, [Pronunciation("New e", "bad"),
                                          Pronunciation("New", "Nju")])
        self.assertEqual(result.text, "Nju e\u0301x")
        self.assertEqual(result.original_text, source)

    def test_regex_metacharacters_are_only_literal_data(self):
        result = prepare_reading("(a+)+$ aaaa", [Pronunciation("(a+)+$", "pattern", False)])
        self.assertEqual(result.text, "pattern aaaa")

    def test_line_endings_are_an_explicit_mapped_derivative(self):
        source = "one\r\ntwo\rthree\nfour"
        unchanged = prepare_reading(source)
        changed = prepare_reading(source, normalize_line_endings=True)
        self.assertEqual(unchanged.text, source)
        self.assertEqual(changed.text, "one\ntwo\nthree\nfour")
        self.assertEqual(changed.source_span(3, 4), (3, 5))
        self.assertEqual(changed.original_text, source)

    def test_deletion_and_insertion_keep_source_span(self):
        result = apply_reading_edits("abc DEF ghi", [ReadingEdit(4, 7, ""), ReadingEdit(11, 11, "!")])
        self.assertEqual(result.text, "abc  ghi!")
        self.assertEqual(result.source_span(0, 8), (0, 11))
        self.assertEqual(result.source_span(8, 9), (11, 11))

    def test_overlap_duplicate_invalid_range_rejected(self):
        for edits in ([ReadingEdit(0, 2, "a"), ReadingEdit(1, 3, "b")],
                      [ReadingEdit(1, 1, "a"), ReadingEdit(1, 1, "b")],
                      [ReadingEdit(True, 2, "b")], [ReadingEdit(-1, 1, "x")],
                      [ReadingEdit(2, 9, "x")]):
            with self.subTest(edits=edits), self.assertRaises(ValueError):
                apply_reading_edits("abc", edits)

    def test_growth_budget_rejected_before_output(self):
        with self.assertRaises(ValueError):
            apply_reading_edits("a" * MAX_READING_CHARACTERS, [ReadingEdit(0, 1, "aa")])

    def test_rule_and_edit_budgets(self):
        with self.assertRaises(ValueError):
            prepare_reading("a", [Pronunciation("a", "b")] * 257)
        with self.assertRaises(ValueError):
            prepare_reading("a " * 10_001, [Pronunciation("a", "b")])

    def test_duplicate_rule_and_bad_range_rejected(self):
        with self.assertRaises(ValueError):
            prepare_reading("a", [Pronunciation("a", "b"), Pronunciation("a", "c")])
        result = prepare_reading("abc")
        for start, end in ((0, 0), (0, 4), (-1, 2), (True, 2)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                result.source_span(start, end)

    def test_immutable_result(self):
        result = prepare_reading("original")
        with self.assertRaises(AttributeError):
            result.text = "changed"


if __name__ == "__main__":
    unittest.main()
