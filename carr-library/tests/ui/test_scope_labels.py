"""Pure shared labels: no native GUI or service authorization claim."""
from itertools import permutations
import unicodedata

from carr_library.ui.scope_bindings import scope_labels


def test_unique_unicode_labels_unchanged():
    assert scope_labels(iter([('a', None, 'Бібліотека'), ('a', 't', 'Výskum')])) == [
        'Бібліотека', 'Výskum']


def test_library_and_topic_none_disambiguation():
    labels = scope_labels([('a', None, 'Same'), ('a', 'none', 'Same')])
    assert labels == ['Same (library a; library only)', 'Same (library a; topic none)']


def test_generated_label_collision_in_both_orders():
    rows = [('a', 'one', 'Same'), ('a', 'two', 'Same'),
            ('a', 'three', 'Same (library a; topic one)')]
    for ordered in (rows, list(reversed(rows))):
        labels = scope_labels(ordered)
        assert len(labels) == len(set(labels)) == 3
        assert all(label.startswith(row[2]) for row, label in zip(ordered, labels))


def test_empty_choices():
    assert scope_labels([]) == []


def test_canonical_generated_label_collisions_preserve_names_and_order():
    rows = [('a', 'one', 'Café'), ('a', 'two', 'Cafe\u0301'),
            ('a', 'three', 'CAFÉ (LIBRARY a; TOPIC one)'),
            ('a', 'four', 'Café (library a; topic one) (choice 2)')]
    for ordered in permutations(rows):
        labels = scope_labels(ordered)
        keys = {' '.join(unicodedata.normalize('NFC', label).split()).casefold() for label in labels}
        assert len(keys) == len(ordered)
        assert all(label.startswith(row[2]) for row, label in zip(ordered, labels))


def test_case_equivalent_identifiers_get_distinct_presentation_without_retargeting():
    labels = scope_labels([('a', None, 'Same'), ('A', None, 'same')])
    assert labels == ['Same (library a; library only)', 'same (library A; library only) (choice 2)']
