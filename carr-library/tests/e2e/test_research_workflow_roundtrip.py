"""Whole research workflow using real SQLite, source bytes and backup restore.

This is an independent service integration check, not Windows/NVDA evidence.
"""
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import restore_backup


def test_result_modes_metadata_tags_and_backup_keep_the_same_historical_quote(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Research library', 'Synthetic sources only')
    other = app.create_workspace('Separate library')
    parent = app.create_topic(workspace, 'Transport')
    topic = app.create_topic(workspace, 'Evidence', parent=parent)
    other_topic = app.create_topic(other, 'Evidence')
    text = (
        'Historical context with unrelated details. ' * 35
        + '\r\nThe railway connected cities in Україна. This is the target sentence.\r\n'
        + 'Adjacent source context is preserved exactly. ' * 30
    )
    raw = text + '\fNEXT SEGMENT MUST STAY SEPARATE.'
    imported = app.add_text(workspace, 'Original title', raw,
                            authors=('Original Author',), language='uk', edition='First')
    app.add_text(other, 'Original title', 'The railway connected other cities.',
                 authors=('Different Author',))

    short, = app.search(workspace, 'railway connected', result_mode='short')
    expanded, = app.search(workspace, 'railway connected', result_mode='thematic')
    assert 'railway connected' in short.quote
    assert len('railway connected') < len(short.quote) < len(expanded.quote)
    assert short.quote in text and expanded.quote in text
    assert 'NEXT SEGMENT' not in expanded.quote
    assert short.verified is expanded.verified is True
    assert app.copy(workspace, short.ref, short.revision, 'quote') == short.quote
    assert app.copy(workspace, expanded.ref, expanded.revision, 'quote') == expanded.quote
    assert app.context(workspace, expanded.ref, expanded.revision).page(0).quote == expanded.quote

    evidence = app.save_hit(workspace, topic, expanded.ref, expanded.revision,
                            'Separate research note, not original source text.')
    app.set_evidence_tags(workspace, topic, evidence, ('transport', 'Україна', 'transport'))
    assert set(app.evidence_tags(workspace, topic, evidence)) == {'transport', 'Україна'}
    assert app.evidence_with_tag(workspace, topic, 'transport')[0].evidence_id == evidence
    assert app.evidence(other, other_topic) == ()
    before = app.evidence(workspace, topic)[0]
    original_asset = app.assets.resolve(imported['asset']).read_bytes()

    app.update_metadata(workspace, imported['edition'], title='Corrected title',
                         authors=('Corrected Author',), language='uk', edition='Second')
    current, = app.search(workspace, 'railway connected', result_mode='thematic')
    assert current.title == 'Corrected title' and current.author == 'Corrected Author'
    assert current.revision != expanded.revision
    assert app.evidence(workspace, topic)[0] == before
    assert app.copy(workspace, expanded.ref, expanded.revision, 'citation') == expanded.citation
    assert app.assets.resolve(imported['asset']).read_bytes() == original_asset
    exported = app.export_topic(workspace, topic, format='text')
    exported_quote = next(line.removeprefix('Exact quotation: ')
                          for line in exported.splitlines() if line.startswith('Exact quotation: '))
    assert json.loads(exported_quote) == expanded.quote and 'Original Author' in exported
    assert 'Corrected Author' not in exported

    app.archive_workspace(workspace)
    with pytest.raises(ValueError):
        app.evidence(workspace, topic)
    app.restore_workspace(workspace)
    backup = tmp_path / 'backup'
    checksum = app.backup(backup)
    restored = tmp_path / 'restored'
    restore_backup(backup, restored, expected_manifest_sha256=checksum)
    reopened = LibraryApp(restored)
    assert reopened.evidence(workspace, topic)[0] == before
    assert set(reopened.evidence_tags(workspace, topic, evidence)) == {'transport', 'Україна'}
    assert reopened.export_topic(workspace, topic, format='text') == exported
    assert reopened.evidence(other, other_topic) == ()
    recovered, = reopened.search(workspace, 'railway connected', result_mode='thematic')
    assert recovered.quote == current.quote
    assert reopened.copy(workspace, recovered.ref, recovered.revision, 'quote') == recovered.quote

