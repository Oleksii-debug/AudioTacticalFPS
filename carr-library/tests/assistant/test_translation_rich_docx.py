"""Real retained rich DOCX, existing translation jobs and protected exports."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import importlib.util
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
from zipfile import ZipFile

from docx import Document
from lxml import etree
import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import translation_docx_rich as rich
from carr_library.assistant import translation_layout as layout
from carr_library.assistant.models import canonical
from carr_library.assistant.profiles import ProfileStore
from carr_library.assistant.providers import Gateway
from carr_library.assistant.translation import TranslationService
from carr_library.assistant.translation_documents import run_documents
from carr_library.assistant.translation_partition import partition_ranges
from carr_library.extract.docx_ordered import extract_docx
from carr_library.library_backup import create_backup, restore_backup
from carr_library.operations.broker import ResourceBroker
from carr_library.operations.models import ResourceStateError

_spec = importlib.util.spec_from_file_location('rich_docx_current_transport',
    Path(__file__).with_name('test_translation_layout.py'))
_transport = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_transport)
ready = _transport.ready
_fixture_path = Path(__file__).resolve().parents[2] / 'scripts' / 'translation_rich_docx_fixtures.py'
_spec = importlib.util.spec_from_file_location('rich_docx_package_fixture', _fixture_path)
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)


@pytest.fixture
def bound(tmp_path, ready):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Selected rich DOCX')
    source = tmp_path / 'Retained images and notes Ω.docx'
    original = fixture.rich_docx(proof=True)
    source.write_bytes(original)
    result = app._import_file(workspace, source, title='Retained note and image source',
        work_id='rich-docx-work', edition_id='rich-docx-edition')
    assert result['state'] == 'complete' and result['verified'] is False
    directory = app.root / 'state' / 'translation'
    service = TranslationService(directory, library=app, gateway=Gateway(directory / 'provider'))
    ProfileStore(app.root / 'state' / 'providers').save(ready.profile)
    service.refresh_languages(ready.profile, authorization=_transport._transport.discovery(ready.profile))
    return SimpleNamespace(app=app, workspace=workspace, edition='rich-docx-edition',
        source=source, original=original, service=service, profile=ready.profile, state=ready.state)


def captured(case):
    return layout.capture_layout(case.app, case.workspace, case.edition)


def prepared(case, selected):
    report = layout.review_layout(case.service, case.profile, selected, 'en', 'uk', '0')
    result = layout.prepare_layout(case.service, case.profile, selected, 'en', 'uk', '0',
                                   approved_review=report['review_sha256'])
    return result['jobs']


def completed(case):
    selected = captured(case)
    identities = prepared(case, selected)
    assert run_documents(case.service, identities)['status'] == 'complete'
    return selected, identities


def test_actual_worker_matches_retained_rich_source_and_exact_owned_origin(bound, monkeypatch):
    invocations = []
    actual = layout.run_process
    def observe(args, **kwargs):
        invocations.append((tuple(args), dict(kwargs)))
        return actual(args, **kwargs)
    monkeypatch.setattr(layout, 'run_process', observe)
    calls = list(bound.state['calls'])
    selected = captured(bound)
    assert selected.mapping['schema'] == rich.PROFILE
    assert len(selected.mapping['records']) == len(selected.selections[0].document.source_anchor['positions']) == 7
    assert selected.summary()['canonical_paragraphs'] == 7 and selected.summary()['note_paragraphs'] == 3
    assert selected.mapping['source_sha256'] == sha256(bound.original).hexdigest()
    assert selected.source_partition['profile'] == rich.PROFILE
    assert len(invocations) == 1 and '-I' in invocations[0][0] and '-B' in invocations[0][0]
    from carr_library.assistant import translation_layout_worker
    assert str(Path(translation_layout_worker.__file__).resolve()) in invocations[0][0]
    assert invocations[0][1]['owned_windows_job'] is (os.name == 'nt')
    assert bound.state['calls'] == calls
    assert ResourceBroker.for_app(bound.app.root).snapshot()['leases'] == []


def test_one_existing_job_covers_full_body_notes_and_separator_text_once(bound):
    selected = captured(bound)
    before = list(bound.state['calls'])
    identities = prepared(bound, selected)
    assert len(identities) == 1 and bound.state['calls'] == before
    job = bound.service.get(identities[0])
    original = '\n\n'.join(row['raw_text'] for row in extract_docx(bound.original)['records'])
    assert job['source_text'] == original and ''.join(job['chunks']) == original
    intervals = partition_ranges(original, job['source_partition'])
    assert ''.join(original[first:last] for first, last in intervals) == original
    assert sum(bool(chunk.strip()) for chunk in job['chunks']) == selected.mapping['selected']
    assert job['source_partition']['mapping_sha256'] == selected.mapping_sha256
    assert job['kind'] == 'translation/2' and job['source_binding']['schema'] == 'carr-translation-source/1'


def test_actual_translation_preserves_note_anchors_images_styles_and_exact_provider_receipts(bound, tmp_path):
    selected, identities = completed(bound)
    path = tmp_path / 'Translated rich Ω.docx'
    result = layout.export_layout(bound.service, selected, identities, path)
    assert result['status'] == 'complete' and result['quotation_authority'] is False
    assert result['changed_members'] == ['word/document.xml', 'word/endnotes.xml', 'word/footnotes.xml']
    assert len(result['jobs']) == selected.mapping['selected'] and len({r['id'] for r in result['jobs']}) == 1
    final = rich.describe(path.read_bytes())
    assert final['media'] == selected.mapping['media']
    expected_texts = []
    for row, exported in zip(selected.mapping['records'], final['records']):
        text, previous = '', 0
        for unit in row['units']:
            value = 'DERIVED: ' + row['text'][unit['start']:unit['end']] + ' &amp;'
            expected_texts.append(value)
            text += row['text'][previous:unit['start']] + value
            previous = unit['end']
        text += row['text'][previous:]
        assert exported['text'] == text and exported['locator'] == row['locator']
        assert [{k: v for k, v in m.items() if k != 'offset'} for m in exported['markers']] == [
            {k: v for k, v in m.items() if k != 'offset'} for m in row['markers']]
    job = bound.service.get(identities[0])
    assert [receipt['text'] for chunk, receipt in zip(job['chunks'], job['receipts']) if chunk.strip()] == expected_texts
    assert [receipt['translation_sha256'] for receipt in result['jobs']] == [
        sha256(value.encode('utf-8')).hexdigest() for value in expected_texts]
    assert all(len(receipt['receipt_indices']) == 1 for receipt in result['jobs'])
    with ZipFile(BytesIO(bound.original)) as original, ZipFile(path) as translated:
        assert original.namelist() == translated.namelist()
        assert all(original.read(name) == translated.read(name) for name in original.namelist()
                   if name not in result['changed_members'])
        assert translated.read('word/media/image1.png') == fixture.png_bytes()
    assert bound.source.read_bytes() == bound.original
    assert bound.app.assets.resolve(selected.asset_sha256).read_bytes() == bound.original


@pytest.mark.parametrize('fault', ['unit-gap', 'unit-overlap', 'note-marker', 'image-digest'])
def test_forged_mapping_never_becomes_a_new_approval_or_request(bound, fault):
    selected = captured(bound)
    mapping = deepcopy(selected.mapping)
    if fault == 'unit-gap':
        mapping['records'][0]['units'][0]['start'] += 1
    elif fault == 'unit-overlap':
        mapping['records'][0]['units'][1]['start'] -= 1
    elif fault == 'note-marker':
        mapping['records'][0]['markers'][0]['note_id'] = 6
    else:
        mapping['media'][0]['sha256'] = '0' * 64
    forged = replace(selected, mapping_json=canonical(mapping))
    calls = list(bound.state['calls'])
    with pytest.raises(ValueError):
        layout.review_layout(bound.service, bound.profile, forged, 'en', 'uk', '0')
    assert bound.state['calls'] == calls and bound.service.list() == ()


def test_pause_resumes_without_repeating_accepted_rich_fragments(bound):
    selected = captured(bound)
    identities = prepared(bound, selected)
    stopping = False
    def progress(done, total, phase):
        nonlocal stopping
        if done == 1:
            stopping = True
    before = len(bound.state['calls'])
    result = bound.service.resume(identities[0], cancelled=lambda: stopping, progress=progress)
    assert result['status'] == 'paused'
    accepted = deepcopy(bound.service.get(identities[0])['receipts'])
    assert len(accepted) == 1 and len(bound.state['calls']) == before + 1
    assert run_documents(bound.service, identities)['status'] == 'complete'
    assert bound.service.get(identities[0])['receipts'][:1] == accepted
    assert len(bound.state['calls']) - before == selected.mapping['selected']


def test_restore_reopens_actual_retained_rich_source_and_exports_complete_jobs_offline(bound, tmp_path, monkeypatch):
    selected, identities = completed(bound)
    first = tmp_path / 'Before restore.docx'
    layout.export_layout(bound.service, selected, identities, first)
    expected = first.read_bytes()
    digest = create_backup(bound.app, tmp_path / 'backup')
    bound.source.unlink()
    first.unlink()
    shutil.rmtree(bound.app.root)
    restored = restore_backup(tmp_path / 'backup', tmp_path / 'restored', digest)
    app = LibraryApp(restored)
    ProfileStore(app.root / 'state/providers').remove(bound.profile.id)
    service = TranslationService(app.root / 'state/translation', library=app)
    def forbidden(*args, **kwargs):
        pytest.fail('Completed rich DOCX export must not contact a translator or vault.')
    monkeypatch.setattr(service.gateway, 'request', forbidden)
    monkeypatch.setattr(service.gateway.vault, 'get', forbidden)
    with pytest.raises(ValueError):
        layout.export_layout(service, selected, identities, tmp_path / 'stale.docx')
    fresh = layout.capture_layout(app, bound.workspace, bound.edition)
    result = layout.export_layout(service, fresh, identities, tmp_path / 'Restored.docx')
    assert result['status'] == 'complete' and (tmp_path / 'Restored.docx').read_bytes() == expected
    assert not (tmp_path / 'stale.docx').exists()


def test_actual_same_byte_competing_output_is_still_a_conflict(bound, tmp_path, monkeypatch):
    selected, identities = completed(bound)
    path = tmp_path / 'Competing.docx'
    captured_bytes = []
    actual = layout.write_binary_export
    def competitor(destination, data, **kwargs):
        Path(destination).write_bytes(data)
        captured_bytes.append(data)
        return actual(destination, data, **kwargs)
    monkeypatch.setattr(layout, 'write_binary_export', competitor)
    with pytest.raises(FileExistsError):
        layout.export_layout(bound.service, selected, identities, path)
    assert captured_bytes == [path.read_bytes()]


def test_literal_translated_note_and_xml_like_text_never_creates_objects(bound, tmp_path):
    import json
    bound.state['raw'] = json.dumps({'translatedText': '<w:footnoteReference w:id="99"/> &amp; literal Ω'}).encode()
    selected, identities = completed(bound)
    path = tmp_path / 'Literal.docx'
    layout.export_layout(bound.service, selected, identities, path)
    final = rich.describe(path.read_bytes())
    assert final['media'] == selected.mapping['media']
    assert [n['note_id'] for n in final['notes']] == [5, 9]
    assert all('<w:footnoteReference w:id="99"/>' in row['text']
               for row in final['records'] if row['selected'])


def test_changed_retained_picture_bytes_refuse_source_before_transfer(bound):
    selected = captured(bound)
    asset = bound.app.assets.resolve(selected.asset_sha256)
    changed = fixture.replace_members(bound.original, {'word/media/image1.png': fixture.png_bytes()[:-1] + b'X'})
    asset.write_bytes(changed)
    before = list(bound.state['calls'])
    with pytest.raises((ValueError, OSError)):
        layout.review_layout(bound.service, bound.profile, selected, 'en', 'uk', '0')
    assert bound.state['calls'] == before and bound.service.list() == ()


def test_source_root_replacement_during_format_process_refuses_late_admission(bound, tmp_path, monkeypatch):
    actual = layout.run_process
    old_root, moved = bound.app.root, tmp_path / 'owned moved root'
    replacement = tmp_path / 'preserved replacement root'
    marker = b'The replacement directory belongs to another owner.\n'
    swapped, child_returned = False, False
    blocked = []
    def replace_root(args, **kwargs):
        nonlocal swapped, child_returned
        result = actual(args, **kwargs)
        child_returned = True
        try:
            old_root.rename(moved)
        except PermissionError as exc:
            if os.name != 'nt' or getattr(exc, 'winerror', None) not in (5, 32):
                raise
            # A real Windows handle may prevent substitution altogether.
            # Preserve that exact OS refusal instead of inventing a swap.
            blocked.append(exc)
            raise
        swapped = True
        old_root.mkdir()
        (old_root / 'replacement-owner.txt').write_bytes(marker)
        return result
    monkeypatch.setattr(layout, 'run_process', replace_root)
    before = list(bound.state['calls'])
    try:
        with pytest.raises((ValueError, OSError, ResourceStateError)) as refused:
            captured(bound)
        assert child_returned and bound.state['calls'] == before
        if swapped:
            assert not blocked
            assert isinstance(refused.value, ResourceStateError)
            assert str(refused.value) == (
                'The selected resource directory changed; reopen the selected library before retrying.')
            causes, seen, error = [], set(), refused.value
            while error is not None and id(error) not in seen:
                seen.add(id(error))
                causes.append(error)
                error = error.__cause__ or error.__context__
            assert any(isinstance(error, ValueError)
                       and 'selected library directory changed' in str(error).lower()
                       for error in causes)
            assert (old_root / 'replacement-owner.txt').read_bytes() == marker
            assert tuple(path.name for path in old_root.iterdir()) == ('replacement-owner.txt',)
        else:
            assert len(blocked) == 1 and refused.value is blocked[0]
            assert old_root.is_dir() and not moved.exists()
            assert ResourceBroker.for_app(bound.app.root).snapshot()['leases'] == []
    finally:
        if swapped:
            # Restore the fixture without deleting another directory's bytes
            # or masking the original ownership failure with rmdir(ENOTEMPTY).
            if old_root.exists():
                old_root.rename(replacement)
            moved.rename(old_root)


def test_actual_commit_has_no_late_source_or_cancellation_observer(bound, tmp_path, monkeypatch):
    selected, identities = completed(bound)
    actual = layout.write_binary_export
    published = False
    def commit(path, data, **kwargs):
        nonlocal published
        result = actual(path, data, **kwargs)
        published = True
        return result
    def cancelled():
        if published:
            pytest.fail('A new observer ran after the actual completed publication.')
        return False
    monkeypatch.setattr(layout, 'write_binary_export', commit)
    result = layout.export_layout(bound.service, selected, identities,
                                  tmp_path / 'Committed.docx', cancelled=cancelled)
    assert published and result['status'] == 'complete'
    assert (tmp_path / 'Committed.docx').is_file()


def test_private_output_destination_stays_protected_for_rich_documents(bound):
    selected, identities = completed(bound)
    destination = bound.app.root / 'Never overwrite library.docx'
    with pytest.raises((ValueError, PermissionError)):
        layout.export_layout(bound.service, selected, identities, destination)
    assert not destination.exists()


def test_rich_source_inventory_is_searchable_but_never_auto_verified(bound):
    hits = bound.app.search(bound.workspace, 'Independent footnote prose')
    assert hits and all(not hit.verified for hit in hits)
    selected = captured(bound)
    assert selected.selections[0].document.source_text.endswith('Independent endnote prose Česko.')
    assert selected.summary()['quotation_authority'] is False


def test_rich_unit_budget_is_enforced_before_any_translation(bound):
    original = fixture.rich_docx()
    with ZipFile(BytesIO(original)) as archive:
        root = etree.fromstring(archive.read('word/document.xml'))
    body = root.find(fixture.W + 'body')
    from docx.oxml import OxmlElement
    for index in range(2049):
        p, r, t = OxmlElement('w:p'), OxmlElement('w:r'), OxmlElement('w:t')
        t.text = 'Extra original unit ' + str(index)
        r.append(t); p.append(r)
        body.insert(len(body) - 1, p)
    raw = fixture.replace_members(original, {'word/document.xml': etree.tostring(root, encoding='utf-8')})
    before = list(bound.state['calls'])
    with pytest.raises(ValueError):
        rich.describe(raw)
    assert bound.state['calls'] == before
