"""Independent synthetic topic-tree data contract audit, not native UI/NVDA.
Set CARR_TOPIC_SCHEMA to the exact PR49 test-only migration file.
"""
import os
import sqlite3
from pathlib import Path
import pytest
from carr_library.research.topic_tree import TopicTree


@pytest.fixture
def trees():
    db = sqlite3.connect(':memory:', isolation_level=None)
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('CREATE TABLE workspace(id TEXT PRIMARY KEY)')
        db.executemany('INSERT INTO workspace VALUES(?)', [('a',), ('b',)])
        db.executescript(Path(os.environ['CARR_TOPIC_SCHEMA']).read_text(encoding='utf-8'))
        db.execute('BEGIN IMMEDIATE')
        yield TopicTree(db, 'a'), TopicTree(db, 'b')
    finally:
        db.close()


@pytest.mark.parametrize('label', ['\u200b', '\u2060', '\x01'])
def test_create_rejects_control_only_name(trees, label):
    a, _ = trees
    with pytest.raises(ValueError):
        a.create('blank', label)
    assert a.list_topics() == ()


def test_rename_rejects_control_only_name(trees):
    a, _ = trees
    a.create('root', 'History')
    with pytest.raises(ValueError):
        a.rename('root', '\u200b\u2060')
    assert a.list_topics()[0].name == 'History'


def test_readable_multilingual_name_preserved(trees):
    a, _ = trees
    label = 'Історія — Železnica — می\u200cرود'
    a.create('root', label)
    assert a.list_topics()[0].name == label


def test_duplicate_names_require_stable_scoped_identity(trees):
    a, b = trees
    a.create('r', 'History')
    a.create('one', 'Notes', parent='r', position=2)
    a.create('two', 'Notes', parent='r', position=1)
    b.create('one', 'Notes')
    a.rename('one', 'Research notes')
    assert [(t.id, t.name) for t in a.list_topics() if t.parent == 'r'] == [
        ('two', 'Notes'), ('one', 'Research notes')]
    assert [(t.id, t.name) for t in b.list_topics()] == [('one', 'Notes')]


def test_archive_descendant_visibility_and_direct_flag(trees):
    # Characterizes API: archived is a direct flag, not effective visibility.
    # Host must use active list membership / ancestry in archive view.
    a, _ = trees
    a.create('r', 'History')
    a.create('c', 'Chapter', parent='r')
    a.create('n', 'Notes', parent='c')
    a.archive('c')
    assert [t.id for t in a.list_topics()] == ['r']
    all_rows = {t.id: t for t in a.list_topics(include_archived=True)}
    assert all_rows['c'].archived and not all_rows['n'].archived
    with pytest.raises(ValueError):
        a.rename('n', 'Unexpected')
