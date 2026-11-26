"""Pure shared labels: no native GUI or service authorization claim."""
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


import unicodedata
import pytest


@pytest.mark.parametrize('names', [
    ('Café', 'Cafe\u0301'),
    ('Історія', 'Історія '),
    ('Research  methods', 'Research methods'),
])
def test_presentation_collision(names):
    rows = [('a', 'one', names[0]), ('a', 'two', names[1])]
    labels = scope_labels(rows)
    assert len(set(labels)) == 2
    assert len({' '.join(unicodedata.normalize('NFC', x).split()) for x in labels}) == 2
    assert all(label.startswith(row[2]) for row, label in zip(rows, labels))
