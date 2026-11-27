"""Earlier-format notes are read-only review material, never inferred evidence."""
from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

from carr_library.durable_state import atomic_json
from carr_library.evidence_drafts import read_legacy_research_note


def seed(root, *, note='Earlier unsaved note 😀\r\nKeep its exact text.'):
    path = root / 'state' / 'recovery' / 'research-note.json'
    value = dict(schema=1, workspace='workspace', topic='topic', evidence='evidence', note=note)
    atomic_json(path, value)
    return path, value


def read(root, **scope):
    return read_legacy_research_note(root,
        **(dict(workspace='workspace', topic='topic', evidence='evidence') | scope))


def test_legacy_writer_bytes_are_reviewed_without_migration_or_source_authority(tmp_path):
    path, old = seed(tmp_path)
    before = path.read_bytes()
    sentinel = tmp_path / 'evidence-drafts.json'
    sentinel.write_bytes(b'Existing modern recovery bytes must not change.')
    current = sentinel.read_bytes()
    value = read(tmp_path)
    assert value.text == old['note']
    assert (value.workspace, value.topic, value.evidence) == ('workspace', 'topic', 'evidence')
    assert value.snapshot_sha256 == sha256(before).hexdigest()
    assert not hasattr(value, 'source_sha256') and not hasattr(value, 'baseline_sha256')
    assert path.read_bytes() == before and sentinel.read_bytes() == current


@pytest.mark.parametrize('field', ['workspace', 'topic', 'evidence'])
def test_legacy_scope_mismatch_never_attaches_or_removes_the_copy(tmp_path, field):
    path, _ = seed(tmp_path)
    before = path.read_bytes()
    assert read(tmp_path, **{field: 'another'}) is None
    assert path.read_bytes() == before


@pytest.mark.parametrize('case', ['duplicate', 'schema_bool', 'extra', 'wrong_text', 'oversize'])
def test_untrusted_legacy_file_is_retained_after_rejection(tmp_path, case):
    path, value = seed(tmp_path)
    if case == 'duplicate':
        raw = b'{"schema":1,"schema":1,"workspace":"workspace","topic":"topic","evidence":"evidence","note":"x"}'
    elif case == 'oversize':
        raw = b' ' * (1024 * 1024 + 4097)
    else:
        if case == 'schema_bool': value['schema'] = True
        elif case == 'extra': value['source_authority'] = True
        else: value['note'] = ['not text']
        raw = json.dumps(value).encode('utf-8')
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        read(tmp_path)
    assert path.read_bytes() == raw


def test_legacy_hardlink_is_not_followed_or_rewritten(tmp_path):
    path, _ = seed(tmp_path)
    alias = tmp_path / 'outside-copy.json'
    os.link(path, alias)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        read(tmp_path)
    assert path.read_bytes() == alias.read_bytes() == before


def test_replacement_between_inspection_and_open_does_not_change_review_identity(tmp_path, monkeypatch):
    from carr_library import durable_state
    path, value = seed(tmp_path)
    replacement = path.with_name('replacement.json')
    value['note'] = 'A later writer produced different bytes.'
    atomic_json(replacement, value)
    changed = replacement.read_bytes()
    original = durable_state._open_json_file
    def swapped(selected):
        replacement.replace(path)
        return original(selected)
    monkeypatch.setattr(durable_state, '_open_json_file', swapped)
    with pytest.raises(ValueError, match='changed while opening'):
        read(tmp_path)
    assert path.read_bytes() == changed


def test_no_earlier_file_has_no_creation_side_effect(tmp_path):
    assert read(tmp_path) is None
    assert not (tmp_path / 'state').exists()
