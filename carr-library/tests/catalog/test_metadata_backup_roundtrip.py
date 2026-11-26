"""Metadata corrections and historical evidence survive a full local restore."""
from hashlib import sha256

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup


def test_shared_work_metadata_correction_survives_backup_without_citation_drift(tmp_path):
    app = LibraryApp(tmp_path / 'original-library')
    workspace = app.create_workspace('Corrected research')
    other = app.create_workspace('Independent research')
    topic = app.create_topic(workspace, 'Historical citations')
    other_topic = app.create_topic(other, 'Independent citations')
    old_authors = ('Original Author', 'Другий автор')
    texts = {
        'e1': 'Exact historical passage for e1.\r\nStraße — Україна.',
        'e2': 'Exact historical passage for e2.\r\nІсторія — Žilina.',
    }
    for identity, language, label, volume in (
        ('e1', 'en', 'First edition', 'I'),
        ('e2', 'uk', 'Second edition', 'II'),
    ):
        imported = app.add_text(workspace, 'Original title', texts[identity],
            authors=old_authors, language=language, edition=label, volume=volume,
            work_id='shared-work', edition_id=identity)
        assert imported['state'] == 'complete'
    # Same work/edition IDs and identical retained bytes in another workspace
    # must not make that workspace part of the shared-work metadata correction.
    imported = app.add_text(other, 'Original title', texts['e1'],
        authors=old_authors, language='en', edition='Independent edition', volume='IX',
        work_id='shared-work', edition_id='e1')
    assert imported['state'] == 'complete'
    old_hits = {hit.edition_id: hit for hit in app.search(workspace, 'historical passage')}
    assert set(old_hits) == {'e1', 'e2'}
    for identity, hit in old_hits.items():
        app.save_hit(workspace, topic, hit.ref, hit.revision, f'Original note for {identity}')
    other_hit, = app.search(other, 'historical passage')
    app.save_hit(other, other_topic, other_hit.ref, other_hit.revision, 'Independent old attribution')
    saved = app.evidence(workspace, topic)
    other_saved = app.evidence(other, other_topic)
    old_export = app.export_topic(workspace, topic, format='text')
    other_export = app.export_topic(other, other_topic, format='text')
    other_catalog = app.browse(other)
    originals = {path.name: path.read_bytes() for path in app.assets_dir.iterdir()}
    assert len(originals) == 2  # The duplicate across workspaces shares retained bytes.

    expected = app.edition_metadata(workspace, 'e1')
    corrected_authors = ('Corrected Author', 'Žan Nový')
    new_revision = app.update_metadata(workspace, 'e1', expected_metadata=expected,
        title='Corrected title', authors=corrected_authors, language='sk',
        edition='Revised first edition', volume='III')
    corrected_catalog = {view.edition_id: view for view in app.browse(workspace)}
    assert all(view.title == 'Corrected title' and view.authors == corrected_authors
               for view in corrected_catalog.values())
    assert (corrected_catalog['e1'].language, corrected_catalog['e1'].edition,
            corrected_catalog['e1'].volume) == ('sk', 'Revised first edition', 'III')
    assert (corrected_catalog['e2'].language, corrected_catalog['e2'].edition,
            corrected_catalog['e2'].volume) == ('uk', 'Second edition', 'II')
    corrected_hits = {hit.edition_id: hit for hit in app.search(workspace,
        'historical passage', author='Corrected Author', title='Corrected title')}
    assert set(corrected_hits) == {'e1', 'e2'}
    assert corrected_hits['e1'].revision == new_revision
    assert all(hit.revision != old_hits[identity].revision for identity, hit in corrected_hits.items())

    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    assert digest == sha256((folder / 'manifest.json').read_bytes()).hexdigest()
    target = restore_backup(folder, tmp_path / 'restored-library', digest)
    restored = LibraryApp(target)

    assert {view.edition_id: view for view in restored.browse(workspace)} == corrected_catalog
    assert restored.browse(other) == other_catalog
    assert restored.evidence(workspace, topic) == saved
    assert restored.evidence(other, other_topic) == other_saved
    assert restored.export_topic(workspace, topic, format='text') == old_export
    assert restored.export_topic(other, other_topic, format='text') == other_export
    assert all(item.anchor.metadata.title == 'Original title' for item in saved)
    assert all(item.anchor.metadata.author == '; '.join(old_authors) for item in saved)

    new_hits = {hit.edition_id: hit for hit in restored.search(workspace,
        'historical passage', author='Corrected Author', title='Corrected title')}
    assert set(new_hits) == {'e1', 'e2'}
    for identity, hit in new_hits.items():
        assert (hit.revision, hit.citation, hit.quote) == (
            corrected_hits[identity].revision, corrected_hits[identity].citation,
            old_hits[identity].quote)
        assert hit.author == '; '.join(corrected_authors) and hit.verified
    assert restored.search(workspace, 'historical passage', author='Original Author') == ()
    unrelated, = restored.search(other, 'historical passage', author='Original Author')
    assert (unrelated.revision, unrelated.citation, unrelated.quote) == (
        other_hit.revision, other_hit.citation, other_hit.quote)
    assert restored.search(other, 'historical passage', author='Corrected Author') == ()
    assert {path.name: path.read_bytes() for path in restored.assets_dir.iterdir()} == originals
    assert {path.name: path.read_bytes() for path in app.assets_dir.iterdir()} == originals
