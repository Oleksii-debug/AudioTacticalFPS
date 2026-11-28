"""Book plan compatibility, actual current Studio effects and passive backup recovery."""
from array import array
from dataclasses import asdict, replace
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import shutil
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import Effect
from carr_library.audio.envelopes import envelope_effect
from carr_library.durable_state import encode, read_json
from carr_library.library_backup import restore_backup
from carr_library.studio.book_audio import BookPlan, BookAudioService, SplitOptions, capture_plan, validate_saved_plan
from carr_library.studio.project import Project, Role, validate_project_effects
from carr_library.studio.render import ProductionService

_spec = importlib.util.spec_from_file_location('book_effects_fixture', Path(__file__).with_name('test_book_audio.py'))
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
book = _fixture.book


def effect_plan(e, effects, **role_options):
    return capture_plan(e.app, e.workspace, e.imported['edition'], topic=e.topic,
        options=SplitOptions('document'),
        role=Role('narrator', 'Narrator', 'synthetic-tone', 'en', effects=effects, **role_options))


def render(e, plan, output=None):
    output = output or e.output
    initial = e.service.review(plan, output)
    ready = e.service.prepare(plan, output, approval=initial.fingerprint)
    rows = e.service.run(plan.id, output, approval=ready.fingerprint)
    assert len(rows) == 1 and rows[0]['status'] == 'complete'
    return e.production.get(plan.job_id(1))


def samples(state):
    with wave.open(str(Path(state['destination']) / 'master.wav'), 'rb') as reader:
        assert reader.getsampwidth() == 2 and reader.getframerate() == 24000
        values = array('h', reader.readframes(reader.getnframes()))
        import sys
        if sys.byteorder != 'little':
            values.byteswap()
        return values[::reader.getnchannels()]


def rms(values):
    return (sum(value * value for value in values) / len(values)) ** .5


def test_original_plan_one_and_project_two_wire_and_job_id_remain_exact(book):
    candidate = _fixture.plan(book, 'document')
    source = candidate.source
    old_plan = {'schema': 1, 'id': candidate.id, 'source': source,
        'options': asdict(candidate.options),
        'role': {'id': 'narrator', 'name': 'Narrator', 'voice_id': 'synthetic-tone',
                 'language': 'en', 'pan': 0, 'gain_db': -3, 'rate': 0, 'volume': 100},
        'parts': [asdict(part) for part in candidate.parts], 'format': 'wav', 'sample_rate': 24000}
    raw = encode(old_plan)
    fingerprint = sha256(raw).hexdigest()
    expected_job = sha256(encode(['book-audio-job', candidate.id, fingerprint, 1])).hexdigest()[:32]
    old_project = {'id': expected_job, 'title': candidate.options.title(1, source['title'], 'Part 1'),
        'roles': (old_plan['role'],),
        'scenes': ({'id': 'part', 'title': 'Part 1', 'lines': (
            {'id': 'line-1', 'role': 'narrator', 'text': source['text'], 'pause_after': 0,
             'source_ref': 'carr-book-audio:%s:%s:1:0:%d' % (candidate.id, fingerprint, len(source['text']))},),
            'music': None, 'cues': (), 'minimum_duration': 0},),
        'original_script': source['text'], 'mode': 'faithful', 'sample_rate': 24000, 'revision': 1, 'schema': 2}
    assert encode(candidate.to_dict()) == raw and candidate.fingerprint == fingerprint
    assert candidate.job_id(1) == expected_job
    assert encode(candidate.project(1).to_dict()) == encode(old_project)
    assert BookPlan.from_dict(json.loads(raw)) == candidate
    assert Project.from_dict(json.loads(encode(old_project))).fingerprint == sha256(encode(old_project)).hexdigest()


def test_explicit_effect_plan_two_roundtrip_keeps_order_bypass_and_immutable_source(book):
    chain = (Effect('gain', gain_db=-6),
             envelope_effect('gain_envelope', [(0, -12), (.1, 0)], interpolation='hold'),
             Effect('lowpass', frequency=6000, enabled=False))
    candidate = effect_plan(book, chain)
    value = candidate.to_dict()
    assert value['schema'] == 2 and candidate.project(1).schema == 3
    assert candidate.project(1).roles[0].effects == chain
    restored = BookPlan.from_dict(json.loads(encode(value)))
    assert restored == candidate and restored.fingerprint == candidate.fingerprint
    assert restored.source['text'] == book.original
    assert restored.provenance(1)['source_anchor']['quotation_authority'] is False
    assert [effect.enabled for effect in restored.role.effects] == [True, True, False]
    assert not book.production.jobs() and not book.voices.received
    changed = dict(value)
    changed['role'] = dict(value['role'], effects=list(reversed(value['role']['effects'])))
    assert BookPlan.from_dict(changed).fingerprint != candidate.fingerprint
    with pytest.raises(TypeError):
        restored.role.effects[0].parameters['gain_db'] = 1


@pytest.mark.parametrize('change', ['legacy-effects', 'empty-v2', 'boolean-schema', 'unknown-schema', 'extra-field'])
def test_saved_schema_or_effect_alias_cannot_reinterpret_existing_plan(book, change):
    candidate = effect_plan(book, (Effect('gain', gain_db=-6),))
    value = json.loads(encode(candidate.to_dict()))
    if change == 'legacy-effects':
        value['schema'] = 1
    elif change == 'empty-v2':
        value['role'].pop('effects')
    elif change == 'boolean-schema':
        value['schema'] = True
    elif change == 'unknown-schema':
        value['schema'] = 3
    else:
        value['automatic_run'] = True
    with pytest.raises(ValueError):
        BookPlan.from_dict(value)
    assert not book.production.jobs() and not book.voices.received


@pytest.mark.parametrize('scope', ['tempo', 'width', 'pan-conflict'])
def test_role_effects_retain_current_timeline_and_channel_scope_guards(book, scope):
    with pytest.raises(ValueError):
        if scope == 'tempo':
            effect_plan(book, (Effect('tempo', factor=1.5),))
        elif scope == 'width':
            effect_plan(book, (envelope_effect('width_envelope', [(0, 1), (.1, .5)]),))
        else:
            effect_plan(book, (envelope_effect('pan_envelope', [(0, -1), (.1, 1)]),), pan=.3)
    assert not book.production.jobs() and not book.voices.received


def test_real_role_gain_uses_current_renderer_receipt_and_cache(book):
    _fixture.require_audio()
    plain = _fixture.plan(book, 'document')
    baseline = render(book, plain)
    prior_speech = tuple(book.voices.received)
    changed = effect_plan(book, (Effect('gain', gain_db=-12),))
    processed = render(book, changed)
    assert tuple(book.voices.received) == prior_speech
    assert any(node['kind'] == 'speech' for node in processed['nodes'].values())
    assert all(node['reused'] for node in processed['nodes'].values() if node['kind'] == 'speech')
    original, quiet = samples(baseline), samples(processed)
    assert len(original) == len(quiet) == 4800
    assert .24 < rms(quiet) / rms(original) < .27
    validate_project_effects(changed.project(1), processed['result'])
    assert processed['result']['effects']['roles']['narrator'] == [effect.to_dict() for effect in changed.role.effects]
    assert processed['result']['book_source_sha256'] == sha256(encode(changed.provenance(1))).hexdigest()
    calls = tuple(book.voices.received)
    before = [(row['path'], row['sha256'], Path(row['path']).stat().st_mtime_ns) for row in processed['artifacts']]
    ready = book.service.review(changed, book.output)
    assert book.service.run(changed.id, book.output, approval=ready.fingerprint)[0]['status'] == 'complete'
    assert tuple(book.voices.received) == calls
    assert before == [(row['path'], row['sha256'], Path(row['path']).stat().st_mtime_ns)
                      for row in book.production.get(changed.job_id(1))['artifacts']]


def test_real_role_hold_envelope_changes_only_measured_samples_and_keeps_source_literal(book):
    _fixture.require_audio()
    baseline = render(book, _fixture.plan(book, 'document'))
    chain = (envelope_effect('gain_envelope', [(0, -18), (.1, -6)], interpolation='hold'),)
    plan = effect_plan(book, chain)
    actual = render(book, plan)
    original, changed = samples(baseline), samples(actual)
    assert len(original) == len(changed) == 4800
    assert .12 < rms(changed[200:2200]) / rms(original[200:2200]) < .14
    assert .49 < rms(changed[2600:4600]) / rms(original[2600:4600]) < .52
    assert all(value == book.original for value in book.voices.received)
    assert plan.project(1).original_script == book.original
    with pytest.raises(ValueError, match='Book|book|source'):
        book.production.run(plan.job_id(1))  # Generic Studio cannot invent the bound source guard.


def test_saved_effect_plan_remains_immutable_before_any_additional_job(book):
    plan = effect_plan(book, (Effect('gain', gain_db=-6),))
    book.service.save(plan)
    original = (book.service.plans / (plan.id + '.json')).read_bytes()
    changed = replace(plan, role=replace(plan.role, effects=(Effect('gain', gain_db=-12),)))
    with pytest.raises(ValueError, match='another reviewed plan'):
        book.service.save(changed)
    assert (book.service.plans / (plan.id + '.json')).read_bytes() == original
    assert not book.production.jobs() and not book.voices.received
    assert validate_saved_plan(read_json(book.service.plans / (plan.id + '.json'))) == plan


def test_effect_plan_real_full_backup_restore_requires_fresh_output_review(book, tmp_path):
    _fixture.require_audio()
    plan = effect_plan(book, (Effect('gain', gain_db=-6),))
    state = render(book, plan)
    original_pcm = samples(state)
    backup = tmp_path / 'effect-plan-backup'
    digest = book.app.backup(backup)
    book.service.close()
    book.production.close()
    shutil.rmtree(book.app.root)
    shutil.rmtree(book.output)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored-effects', digest))
    production = ProductionService(restored.root / 'state' / 'studio', voices=book.voices,
        runtime_factory=book.production.runtime_factory)
    service = BookAudioService(restored, book.workspace, production=production)
    output = tmp_path / 'fresh-reviewed-output'
    output.mkdir()
    try:
        saved = service.load(plan.id)
        assert saved == plan and saved.project(1).schema == 3
        assert production.get(plan.job_id(1))['recovery']['requires_output_directory']
        with pytest.raises(ValueError):
            production.run(plan.job_id(1))
        review = service.review(saved, output)
        ready = service.prepare(saved, output, approval=review.fingerprint)
        with pytest.raises(ValueError, match='review'):
            service.run(plan.id, output, approval=review.fingerprint)
        assert service.run(plan.id, output, approval=ready.fingerprint)[0]['status'] == 'complete'
        actual = production.get(plan.job_id(1))
        assert samples(actual) == original_pcm
        assert Path(actual['destination']).parent == output
    finally:
        service.close()
        production.close()
