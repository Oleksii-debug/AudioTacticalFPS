"""Actual local service calls, source scope and immutable derivative results."""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant.models import canonical
from carr_library.assistant.production import LocalProduction
from carr_library.assistant.validation import validate_job
from carr_library.audio.models import Effect
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.reader.reading_desk import ReadingDesk


@pytest.fixture
def local(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Explicit local operations')
    book = app.add_text(workspace, 'Original', 'First exact page.\fДруга точна сторінка.\r\n')
    inputs, output = tmp_path / 'input', tmp_path / 'output'
    inputs.mkdir(); output.mkdir()
    return app, workspace, book, inputs, output


def _reader_step(app, workspace, book, page=2):
    revision = ReadingDesk(app, workspace, book['edition']).revision
    return Step('reader.page', {'edition': book['edition'], 'revision': revision, 'page': page})


def _run(app, scope, *steps, production=None):
    service = AssistantService(app, production=production)
    identity = service.submit(Plan(scope, steps))
    result = service.run(identity)
    return service, identity, result


def test_reader_uses_exact_current_scope_and_never_creates_a_search_selection(local):
    app, workspace, book, _, output = local
    expected = ReadingDesk(app, workspace, book['edition']).page(2)
    service, identity, result = _run(app, Scope(workspace, output_root=output),
        _reader_step(app, workspace, book))
    assert result.status == 'complete', result
    value = result.results[0]
    assert value['page']['raw_text'] == expected.raw_text
    assert value['page']['location'] == asdict(expected.location)
    assert value['page']['metadata'] == asdict(expected.metadata)
    assert value['quotation_authority'] is False and value['artifacts'] == []
    assert app._selections == {}
    assert service.resume(identity).results == result.results


def test_reader_does_not_retarget_a_changed_revision(local):
    app, workspace, book, _, _ = local
    service = AssistantService(app)
    identity = service.submit(Plan(Scope(workspace), (_reader_step(app, workspace, book),)))
    app.update_metadata(workspace, book['edition'], title='New current title')
    result = service.run(identity)
    assert result.status == 'failed' and not result.results
    assert 'revision' in result.message.lower()
    assert app._selections == {}


def test_reader_never_reads_another_workspaces_original(local, monkeypatch):
    app, workspace, book, _, _ = local
    other = app.create_workspace('Not selected')
    step = _reader_step(app, workspace, book)
    service = AssistantService(app)
    identity = service.submit(Plan(Scope(other), (step,)))
    monkeypatch.setattr(app.assets, 'resolve',
        lambda *a, **k: pytest.fail('Cross-scope step opened an original.'))
    report = service.run(identity)
    assert report.status == 'failed' and not report.results


def test_new_reading_checkpoint_roundtrip_retains_historical_bytes_without_run_authority(local, tmp_path):
    app, workspace, book, _, _ = local
    service, identity, report = _run(app, Scope(workspace), _reader_step(app, workspace, book))
    assert report.status == 'complete'
    raw = service.store.path(identity).read_bytes()
    backup = tmp_path / 'snapshot'
    digest = create_backup(app, backup)
    verify_backup(backup, digest)
    restored = restore_backup(backup, tmp_path / 'restored', digest)
    reopened = AssistantService(LibraryApp(restored))
    assert reopened.store.path(identity).read_bytes() == raw
    assert reopened.get(identity).status == 'attention'
    with pytest.raises(PermissionError):
        reopened.run(identity)


def test_resealed_wrong_reader_text_is_refused_by_existing_complete_backup(local, tmp_path):
    app, workspace, book, _, _ = local
    service, identity, report = _run(app, Scope(workspace), _reader_step(app, workspace, book))
    assert report.status == 'complete'
    value = service.store.load(identity)
    value['steps'][0]['result']['page']['raw_text'] = 'Replaced source text.'
    service.store.save(value)
    destination = tmp_path / 'wrong-backup'
    with pytest.raises((ValueError, RuntimeError), match='reading|source|sealed|sidecar'):
        create_backup(app, destination)
    assert not (destination / 'manifest.json').exists()


def test_evidence_discovery_uses_current_authenticated_notebook_without_copying_quotes(local):
    app, workspace, _, _, _ = local
    hit = app.search(workspace, 'First exact')[0]
    topic = app.create_topic(workspace, 'Research')
    evidence = app.save_hit(workspace, topic, hit.ref, hit.revision, note='Local note')
    service, identity, report = _run(app, Scope(workspace), Step('evidence.search', {'query': 'First'}))
    assert report.status == 'complete', report.message
    value = report.results[0]
    assert value['total_matches'] == 1 and value['truncated'] is False
    assert value['hits'][0]['evidence'] == evidence
    assert value['hits'][0]['source']['workspace'] == workspace
    assert value['quotation_authority'] is False
    assert 'quote' not in value['hits'][0] and 'raw_text' not in value['hits'][0]
    validate_job(service.store.load(identity))


def test_actual_conversion_uses_owned_worker_broker_and_retained_provenance(local, monkeypatch):
    app, _, _, inputs, output = local
    source = inputs / 'Україна.txt'
    raw = 'Original text.\nНезмінне джерело.'.encode('utf-8')
    source.write_bytes(raw)
    from carr_library.operations import ResourceBroker
    from carr_library.conversion import service as conversion
    original = conversion.ConversionService.extract
    admissions = []
    def extract(self, *args, **kwargs):
        assert isinstance(kwargs.get('broker'), ResourceBroker)
        admissions.append(kwargs['broker'])
        return original(self, *args, **kwargs)
    monkeypatch.setattr(conversion.ConversionService, 'extract', extract)
    service, identity, report = _run(app, Scope(input_roots=(inputs,), output_root=output,
        max_bytes=32 * 1024**2, max_files=2),
        Step('document.convert', {'path': str(source), 'name': 'reading.txt'}))
    assert report.status == 'complete', report.message
    assert len(admissions) == 1 and source.read_bytes() == raw
    result = report.results[0]
    assert len(result['artifacts']) == 2 and result['quotation_authority'] is False
    assert (output / 'reading.txt').read_bytes() == raw
    provenance = json.loads((output / 'reading.txt.provenance.json').read_bytes())
    assert provenance['source_sha256'] == sha256(raw).hexdigest()
    monkeypatch.setattr(conversion.ConversionService, 'extract',
        lambda *a, **k: pytest.fail('Completed conversion was repeated.'))
    assert service.resume(identity).status == 'complete'
    (output / 'reading.txt').write_bytes(b'Changed derivative.')
    assert service.resume(identity).status == 'failed'


def test_conversion_never_adopts_an_existing_unrelated_output(local):
    app, _, _, inputs, output = local
    source = inputs / 'source.txt'; source.write_text('Original', encoding='utf-8')
    target = output / 'reading.txt'; target.write_bytes(b'Other work.')
    with pytest.raises(FileExistsError):
        AssistantService(app).submit(Plan(Scope(input_roots=(inputs,), output_root=output),
            (Step('document.convert', {'path': str(source), 'name': target.name}),)))
    assert target.read_bytes() == b'Other work.' and source.read_text() == 'Original'


@pytest.mark.parametrize('tool', ['document.convert', 'player.inspect'])
def test_external_source_is_rejected_before_any_adapter(local, tool):
    app, _, _, inputs, output = local
    outside = output / 'not-selected.wav'; outside.write_bytes(b'Unrelated')
    args = {'path': str(outside), **({'name': 'new.txt'} if tool == 'document.convert' else {})}
    with pytest.raises(PermissionError):
        AssistantService(app).submit(Plan(Scope(input_roots=(inputs,), output_root=output),
                                         (Step(tool, args),)))


def test_effect_chain_is_a_source_preserving_proposal_not_an_edit_or_render(local):
    app, _, _, _, _ = local
    script = '[role Alice]\r\nAlice: Spoken words must stay exact.\r\n'
    effect = Effect('highpass', (('frequency', 90),), False)
    args = {'script': script, 'target': 'Alice', 'effects': [asdict(effect)]}
    _, _, result = _run(app, Scope(), Step('audio.effects', args))
    assert result.status == 'complete', result.message
    changed = result.results[0]
    assert 'Alice: Spoken words must stay exact.\r\n' in changed['script']
    assert '[effect Alice highpass frequency=90 enabled=false]' in changed['script']
    assert changed['original_sha256'] == sha256(script.encode('utf-8')).hexdigest()
    assert changed['draft_modified'] is changed['rendered'] is False
    assert not (app.root / 'audio-projects').exists()


def test_master_restriction_reuses_studio_validation(local):
    app, *_ = local
    step = Step('audio.effects', {'script': 'Narrator: Exact.', 'target': None,
        'effects': [asdict(Effect('tempo', (('factor', 1.2),)))]})
    with pytest.raises(ValueError, match='Tempo|tempo'):
        AssistantService(app).submit(Plan(Scope(), (step,)))


def test_voice_inventory_and_audio_plan_do_not_synthesize_or_load_a_player(local):
    app, *_ = local
    voice = SimpleNamespace(id='sapi5:fixture', name='Fixture local voice', languages=('uk',))
    registry = SimpleNamespace(voices=lambda: (voice,),
        synthesize=lambda *a, **k: pytest.fail('Planning synthesized audio.'))
    production = LocalProduction(app.root, registry=registry)
    _, _, report = _run(app, Scope(), Step('voice.list'), Step('audio.plan', {
        'text': '[pause 2]\nLiteral source.', 'voice_ids': [voice.id], 'language': 'uk'}),
        production=production)
    assert report.status == 'complete', report.message
    assert report.results[0]['voices'] == [{'id': voice.id, 'name': voice.name, 'languages': ['uk']}]
    plan = report.results[1]
    assert plan['rendered'] is False
    assert plan['production_plan']['input_kind'] == 'narration'
    assert plan['production_plan']['original_script'].startswith('[pause 2]')


def test_pcm_inspection_holds_exact_bytes_without_starting_output(local, monkeypatch):
    app, _, _, inputs, _ = local
    path = inputs / 'PCM.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 800, 'NONE', 'not compressed'))
        stream.writeframes(b'\x00\x01' * 800)
    from carr_library.audio.player import WavePlayer
    monkeypatch.setattr(WavePlayer, 'play', lambda *a, **k: pytest.fail('Inspection started playback.'))
    before = path.read_bytes()
    _, _, report = _run(app, Scope(input_roots=(inputs,)), Step('player.inspect', {'path': str(path)}))
    assert report.status == 'complete', report.message
    value = report.results[0]
    assert value['audio']['frames'] == 800 and value['seconds'] == .1
    assert value['sha256'] == sha256(before).hexdigest() and value['playback_started'] is False
    assert path.read_bytes() == before


@pytest.mark.parametrize('tool,args', [
    ('reader.page', {'edition': 'e', 'revision': 'r', 'page': True}),
    ('reader.page', {'edition': 'e', 'revision': 'r', 'page': 1, 'workspace': 'other'}),
    ('evidence.search', {'query': 'term', 'include_archived': 1}),
    ('evidence.search', {'query': 'term', 'limit': 101}),
    ('voice.list', {'limit': True}),
    ('voice.list', {'credential': 'do-not-read'}),
    ('document.convert', {'path': '/chosen', 'name': 'new.txt', 'executable': '/arbitrary'}),
    ('player.inspect', {'path': '/chosen', 'play': True}),
    ('audio.effects', {'script': 'Narrator: Exact.', 'target': 'Narrator', 'effects': [], 'master_mix': .5}),
], ids=['bool-page', 'foreign-scope', 'archive-bool', 'limit', 'voice-limit',
        'credentials', 'executable', 'device-authority', 'role-mix'])
def test_new_tools_reject_extra_authority_and_coercions(local, tool, args):
    app, workspace, _, inputs, output = local
    with pytest.raises((ValueError, PermissionError)):
        AssistantService(app).submit(Plan(Scope(workspace, (inputs,), output), (Step(tool, args),)))


def test_result_budget_and_cancel_leave_no_claimed_completed_step(local):
    app, workspace, book, _, _ = local
    service = AssistantService(app)
    step = _reader_step(app, workspace, book)
    identity = service.submit(Plan(Scope(workspace, max_bytes=10), (step,)))
    assert service.run(identity).status == 'failed'
    other = service.submit(Plan(Scope(workspace), (step,)))
    report = service.run(other, cancelled=lambda: True)
    assert report.status == 'paused' and report.completed == 0
    assert app._selections == {}


@pytest.mark.parametrize('field,value', [('evidence', 'foreign-evidence'), ('checksum', '0' * 64)],
                         ids=['different-notebook-entry', 'different-authenticated-payload'])
def test_backup_binds_discovered_evidence_to_the_actual_saved_notebook_entry(local, tmp_path, field, value):
    app, workspace, _, _, _ = local
    hit = app.search(workspace, 'First exact')[0]
    topic = app.create_topic(workspace, 'Saved evidence')
    app.save_hit(workspace, topic, hit.ref, hit.revision)
    service, identity, report = _run(app, Scope(workspace), Step('evidence.search', {'query': 'First'}))
    assert report.status == 'complete', report.message
    saved = service.store.load(identity)
    saved['steps'][0]['result']['hits'][0][field] = value
    service.store.save(saved)
    destination = tmp_path / 'mismatched-evidence'
    with pytest.raises((ValueError, RuntimeError), match='evidence|notebook|sidecar'):
        create_backup(app, destination)
    assert not (destination / 'manifest.json').exists()
