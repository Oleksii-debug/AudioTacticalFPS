"""Real library/source/receipt adapters under the existing approved assistant job.

No provider, account or model is contacted. Audio cases use the installed FFmpeg
and the existing resource broker; failing prerequisites are not skipped.
"""
from array import array
from dataclasses import asdict, replace
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import sys
import threading
from types import SimpleNamespace
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant import local_tools
from carr_library.library_backup import restore_backup
from carr_library.operations import ResourceBroker, ResourceLimits, ResourceRequest
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.research import export_file
from carr_library.research.saved_evidence import SavedEvidenceStore
from carr_library.speech.runtime import Voice, VoiceRegistry
from carr_library import runtime_settings


@pytest.fixture
def env(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Reviewed scope')
    topic = app.create_topic(workspace, 'Saved evidence')
    other = app.create_workspace('Other private scope')
    foreign_topic = app.create_topic(other, 'Foreign evidence')
    inputs, outputs = tmp_path / 'inputs', tmp_path / 'outputs'
    inputs.mkdir()
    outputs.mkdir()
    original = 'Railway records remain exact.\r\nУкраїнський рядок.\fSecond untouched segment.'
    imported = app.add_text(workspace, 'Original source title', original)
    foreign = app.add_text(other, 'Private source', 'Railway secret text.')
    hit, = app.search(workspace, 'Railway records')
    evidence = app.save_hit(workspace, topic, hit.ref, hit.revision, 'Researcher note, not a quotation.')
    foreign_hit, = app.search(other, 'Railway secret')
    foreign_evidence = app.save_hit(other, foreign_topic, foreign_hit.ref, foreign_hit.revision, 'Secret note')
    scope = Scope(workspace, (inputs,), outputs, max_seconds=60, max_bytes=64 * 1024**2)
    broker = ResourceBroker.for_app(app.root, limits=ResourceLimits(
        cpu_slots=1, memory_bytes=1024**3, disk_bytes=1024**3, min_free_disk_bytes=0))
    return SimpleNamespace(app=app, workspace=workspace, topic=topic, other=other,
        foreign_topic=foreign_topic, evidence=evidence, foreign_evidence=foreign_evidence,
        inputs=inputs, outputs=outputs, original=original, imported=imported,
        foreign=foreign, hit=hit, scope=scope, broker=broker, service=AssistantService(app))


def reading(e, **changes):
    values = {'edition': e.imported['edition'], 'revision': e.hit.revision,
              'first': 1, 'last': 1, 'topic': e.topic}
    return Step('reader.read', {**values, **changes})


def plan(e, *steps, scope=None):
    return Plan(scope or e.scope, tuple(steps))


def execute(e, *steps, **kwargs):
    identity = e.service.submit(plan(e, *steps))
    return identity, e.service.run(identity, **kwargs)


def alter_note(e, note):
    with e.app.write(e.workspace) as db:
        load, metadata, _ = e.app._loaders(db, e.workspace)
        SavedEvidenceStore(db, e.workspace, load, metadata).edit_note(e.topic, e.evidence, note)


def test_exact_reader_result_is_bound_to_real_revision_and_keeps_originals(env):
    e = env
    source_before = e.app.assets.resolve(e.imported['asset']).read_bytes()
    identity, result = execute(e, reading(e, first=2, last=2))
    assert result.status == 'complete', result
    row, = result.results
    assert row['text'] == e.original.split('\f')[1]
    assert row['source_sha256'] == sha256(row['text'].encode()).hexdigest()
    assert row['source_anchor']['positions'][0]['ordinal'] == 2
    assert row['source_anchor']['revision'] == e.hit.revision
    assert row['source_metadata'] == asdict(ReadingDesk(e.app, e.workspace, e.imported['edition']).metadata)
    assert row['quotation_authority'] is False and row['artifacts'] == []
    assert e.app.assets.resolve(e.imported['asset']).read_bytes() == source_before
    assert len(e.app.evidence(e.workspace, e.topic)) == 1
    reopened = AssistantService(LibraryApp(e.app.root))
    assert reopened.resume(identity).results == result.results


@pytest.mark.parametrize('when', ['before', 'completed'])
@pytest.mark.parametrize('change', ['revision', 'topic', 'original'])
def test_reader_refuses_changed_source_before_action_and_cached_reuse(env, when, change):
    e = env
    identity = e.service.submit(plan(e, reading(e)))
    if when == 'completed':
        assert e.service.run(identity).status == 'complete'
    if change == 'revision':
        e.app.update_metadata(e.workspace, e.imported['edition'], title='Different current revision')
    elif change == 'topic':
        with e.app.topic_transaction(e.workspace) as tree:
            tree.archive(e.topic)
    else:
        e.app.assets.resolve(e.imported['asset']).write_bytes(b'Changed retained original.')
    result = e.service.resume(identity)
    assert result.status in ('attention', 'failed'), result
    assert not tuple(e.outputs.iterdir())
    if when == 'before':
        assert result.completed == 0


def test_reader_rechecks_after_real_page_read_before_accepting_result(env, monkeypatch):
    e = env
    from carr_library.speech import source
    actual = source.read_source_range
    def revoke(*args, **kwargs):
        value = actual(*args, **kwargs)
        e.app.archive_workspace(e.workspace)
        return value
    monkeypatch.setattr(source, 'read_source_range', revoke)
    _, result = execute(e, reading(e))
    assert result.status in ('attention', 'failed') and result.completed == 0
    assert not tuple(e.outputs.iterdir())


def test_saved_evidence_search_read_export_use_existing_authentication(env):
    e = env
    identity, result = execute(e,
        Step('evidence.search', {'query': 'Researcher note', 'field': 'note'}),
        Step('evidence.read', {'topic': e.topic, 'evidence': e.evidence}),
        Step('evidence.export', {'topic': e.topic, 'evidence_ids': [e.evidence], 'name': 'selected.md'}))
    assert result.status == 'complete', result
    search, read, exported = result.results
    hit, = search['results']['hits']
    assert hit['evidence']['evidence_id'] == e.evidence
    assert hit['matched_fields'] == ['note']
    assert read['evidence']['anchor']['quote'] == 'Railway records'
    assert read['evidence']['note'] == 'Researcher note, not a quotation.'
    target = e.outputs / 'selected.md'
    saved = target.read_bytes()
    assert b'Railway records' in saved and b'Researcher note' in saved and b'Secret' not in saved
    assert exported['artifacts'][0]['sha256'] == sha256(saved).hexdigest()
    assert len(e.app.evidence(e.workspace, e.topic)) == 1
    assert e.service.resume(identity).results == result.results
    alter_note(e, 'Changed independently after export.')
    changed = e.service.resume(identity)
    assert changed.status in ('attention', 'failed') and target.read_bytes() == saved


@pytest.mark.parametrize('tool', ['evidence.read', 'evidence.export'])
def test_evidence_cannot_read_foreign_workspace_ids(env, tool):
    e = env
    args = {'topic': e.foreign_topic}
    args.update({'evidence': e.foreign_evidence} if tool.endswith('read')
                else {'evidence_ids': [e.foreign_evidence], 'name': 'forbidden.md'})
    _, result = execute(e, Step(tool, args))
    assert result.status in ('attention', 'failed') and result.completed == 0
    assert not tuple(e.outputs.iterdir())


def test_export_rechecks_saved_note_at_actual_publication_boundary(env, monkeypatch):
    e = env
    writer = export_file.write_export
    def edit_then_write(*args, **kwargs):
        alter_note(e, 'Revoked reviewed note before the real writer.')
        return writer(*args, **kwargs)
    monkeypatch.setattr(export_file, 'write_export', edit_then_write)
    _, result = execute(e, Step('evidence.export',
        {'topic': e.topic, 'evidence_ids': [e.evidence], 'name': 'revoked.md'}))
    assert result.status in ('attention', 'failed') and result.completed == 0
    assert not (e.outputs / 'revoked.md').exists()


def test_late_export_cancel_and_optional_observer_error_keep_committed_receipt(env, monkeypatch):
    e = env
    cancelled = threading.Event()
    writer = export_file.write_export
    def published_then_cancel(*args, **kwargs):
        receipt = writer(*args, **kwargs)
        cancelled.set()
        return receipt
    monkeypatch.setattr(export_file, 'write_export', published_then_cancel)
    def observer(*args):
        raise RuntimeError('An optional observer is disconnected.')
    identity, result = execute(e,
        Step('evidence.export', {'topic': e.topic, 'evidence_ids': [e.evidence], 'name': 'accepted.md'}),
        Step('library.catalog'), cancelled=cancelled.is_set, progress=observer)
    assert result.status == 'paused' and result.completed == 1, result
    before = (e.outputs / 'accepted.md').read_bytes()
    def no_repeat(*args, **kwargs):
        pytest.fail('A completed export must never be published twice.')
    monkeypatch.setattr(export_file, 'write_export', no_repeat)
    result = e.service.resume(identity, progress=observer)
    assert result.status == 'complete' and result.completed == 2
    assert (e.outputs / 'accepted.md').read_bytes() == before


def test_unknown_export_attempt_never_adopts_or_overwrites_existing_output(env, monkeypatch):
    e = env
    identity = e.service.submit(plan(e, Step('evidence.export',
        {'topic': e.topic, 'evidence_ids': [e.evidence], 'name': 'uncertain.md'})))
    record = e.service.store.load(identity)
    record['steps'][0]['intent']['export_attempt'] = {'path': str(e.outputs / 'uncertain.md')}
    record['steps'][0]['status'] = 'running'
    e.service.store.save(record)
    foreign = e.outputs / 'uncertain.md'
    foreign.write_bytes(b'Unrelated private bytes')
    def fail(*args, **kwargs):
        pytest.fail('Unknown prior publication cannot be read/adopted or repeated.')
    monkeypatch.setattr(export_file, 'write_export', fail)
    result = e.service.resume(identity)
    assert result.status == 'attention' and result.completed == 0
    assert foreign.read_bytes() == b'Unrelated private bytes'


def test_plan_preview_and_submit_do_not_discover_voices_or_run_native_audio(env, monkeypatch):
    e = env
    def fail(*args, **kwargs):
        pytest.fail('Preview and submit cannot enumerate native tools or voices.')
    monkeypatch.setattr(runtime_settings, 'voice_registry', fail)
    monkeypatch.setattr(runtime_settings, 'audio_runtime', fail)
    for instruction, expected in [('List voices', 'voice.list'), ('Покажи аудіоефекти', 'audio.effects')]:
        reviewed = e.service.preview(instruction, e.scope)
        assert reviewed.steps[0].tool == expected
        identity = e.service.submit(reviewed)
        assert e.service.get(identity).status == 'queued'


def test_actual_registry_inventory_is_owned_bounded_and_never_synthesizes(env, monkeypatch):
    e = env
    calls = []
    class Provider:
        name = 'Explicit synthetic voice enumerator'
        def voices(self):
            calls.append('enumerate')
            return (Voice('test:uk', 'Reviewed Ukrainian voice', 'test', 'uk'),
                    Voice('test:en', 'Reviewed English voice', 'test', 'en'))
        def synthesize(self, *args, **kwargs):
            pytest.fail('Voice inventory does not synthesize or play.')
        def close(self):
            calls.append('close')
    def registry(state_root, *, cancelled):
        assert state_root == e.app.root / 'state'
        assert e.broker.snapshot()['usage']['cpu_slots'] == 1
        return VoiceRegistry([Provider()], cancelled=cancelled)
    monkeypatch.setattr(runtime_settings, 'voice_registry', registry)
    identity, result = execute(e, Step('voice.list', {'language': 'uk'}))
    assert result.status == 'complete', result
    assert [row['id'] for row in result.results[0]['voices']] == ['test:uk']
    assert calls == ['enumerate', 'close']
    assert e.broker.snapshot()['leases'] == []
    assert e.service.resume(identity).status == 'complete'
    assert calls == ['enumerate', 'close']


def test_cancelled_admission_never_starts_voice_discovery(env, monkeypatch):
    e = env
    called = threading.Event()
    def fail(*args, **kwargs):
        called.set()
        pytest.fail('An occupied cancelled lease cannot start a provider.')
    monkeypatch.setattr(runtime_settings, 'voice_registry', fail)
    waiting = threading.Event()
    reserve = ResourceBroker.reserve
    def observed(broker, *args, **kwargs):
        if kwargs['operation_id'].startswith('assistant:'):
            waiting.set()
        return reserve(broker, *args, **kwargs)
    monkeypatch.setattr(ResourceBroker, 'reserve', observed)
    identity = e.service.submit(plan(e, Step('voice.list')))
    result, errors = [], []
    with e.broker.reserve(ResourceRequest(cpu_slots=1), operation_id='held:test'):
        def run():
            try:
                result.append(e.service.run(identity))
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=run)
        worker.start()
        try:
            assert waiting.wait(5)
            e.service.cancel(identity)
        finally:
            worker.join(5)
        assert not worker.is_alive()
    assert errors == [] and result[0].status == 'cancelled'
    assert result[0].completed == 0 and not called.is_set()
    assert e.broker.snapshot()['leases'] == []


def tone(path, frequency=440):
    samples = array('h', [round(4000 * math.sin(2 * math.pi * frequency * index / 24000))
                          for index in range(3000)])
    if sys.byteorder != 'little':
        samples.byteswap()
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'PCM'))
        stream.writeframes(samples.tobytes())
    return path


def conversion(e, sources, name='converted'):
    return Step('audio.convert', {'sources': [{'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
                                               for path in sources],
                                 'name': name, 'format': 'wav',
                                 'settings': {'channels': 1, 'sample_rate': 24000,
                                              'effects': [{'kind': 'gain', 'parameters': {'gain_db': -6}, 'enabled': True}]}})


def test_actual_audio_inspection_effects_and_conversion_keep_exact_source_hashes(env):
    e = env
    source = tone(e.inputs / 'source.wav')
    before = source.read_bytes()
    _, inspected = execute(e, Step('audio.inspect', {'path': str(source)}), Step('audio.effects'))
    assert inspected.status == 'complete', inspected
    measured, controls = inspected.results
    assert measured['source_sha256'] == sha256(before).hexdigest()
    assert measured['audio']['frames'] == 3000 and measured['audio']['sample_rate'] == 24000
    assert 'gain' in controls['typed_controls']
    assert '.wav' in controls['installed_runtime']['formats']
    assert controls['installed_runtime']['local_only'] is True
    identity, converted = execute(e, conversion(e, [source]))
    assert converted.status == 'complete', converted
    result, = converted.results
    output = next(row for row in result['artifacts'] if row['path'].endswith('.wav'))
    with wave.open(output['path'], 'rb') as stream:
        assert stream.getnframes() == 3000 and stream.getframerate() == 24000
        pcm = array('h', stream.readframes(stream.getnframes()))
    if sys.byteorder != 'little':
        pcm.byteswap()
    assert 1900 <= max(pcm) <= 2100
    assert source.read_bytes() == before
    assert e.broker.snapshot()['leases'] == []
    assert e.service.resume(identity).results == converted.results
    Path(output['path']).write_bytes(b'Tampered completed output')
    changed = e.service.resume(identity)
    assert changed.status == 'failed'
    assert Path(output['path']).read_bytes() == b'Tampered completed output'


def test_conversion_hash_revocation_prevents_creation_and_encoder_start(env, monkeypatch):
    e = env
    source = tone(e.inputs / 'source.wav')
    identity = e.service.submit(plan(e, conversion(e, [source])))
    tone(source, frequency=880)
    from carr_library.audio import AudioRuntime
    def fail(*args, **kwargs):
        pytest.fail('Changed reviewed source bytes cannot reach the encoder.')
    monkeypatch.setattr(AudioRuntime, 'convert', fail)
    result = e.service.run(identity)
    assert result.status == 'failed' and result.completed == 0
    assert not (e.outputs / 'converted').exists()


def test_conversion_late_stop_resumes_only_remaining_real_item(env, monkeypatch):
    e = env
    first, second = tone(e.inputs / 'first.wav'), tone(e.inputs / 'second.wav', 880)
    stopped, calls = threading.Event(), []
    from carr_library.audio import AudioRuntime
    actual = AudioRuntime.convert
    def convert(runtime, path, destination, **kwargs):
        calls.append(str(path))
        result = actual(runtime, path, destination, **kwargs)
        if str(path) == str(first):
            stopped.set()
        return result
    monkeypatch.setattr(AudioRuntime, 'convert', convert)
    identity, result = execute(e, conversion(e, [first, second]), cancelled=stopped.is_set)
    assert result.status == 'paused' and result.completed == 0, result
    manifest = json.loads((e.outputs / 'converted' / 'conversion-manifest.json').read_bytes())
    assert manifest['items'][0]['status'] == 'completed'
    assert manifest['items'][1]['status'] == 'pending' and calls == [str(first)]
    preserved = (e.outputs / 'converted' / manifest['items'][0]['output']).read_bytes()
    stopped.clear()
    result = e.service.resume(identity)
    assert result.status == 'complete', result
    assert calls == [str(first), str(second)]
    assert (e.outputs / 'converted' / manifest['items'][0]['output']).read_bytes() == preserved
    assert e.broker.snapshot()['leases'] == []


@pytest.mark.parametrize('step', [
    Step('reader.read', {'edition': 'e', 'revision': 'r', 'first': 1, 'last': 129}),
    Step('evidence.export', {'topic': 't', 'evidence_ids': ['x', 'x'], 'name': 'out'}),
    Step('voice.list', {'command': 'unlisted'}),
    Step('audio.convert', {'sources': [{'path': 'in.wav', 'sha256': '0' * 64}],
                           'name': 'out', 'settings': {'filter': 'amovie=private'}}),
    Step('audio.convert', {'sources': [{'path': 'in.wav', 'sha256': 'invented'}], 'name': 'out'}),
])
def test_typed_arguments_reject_expansion_before_runtime(env, step):
    with pytest.raises(ValueError):
        env.service.submit(plan(env, step))


def test_real_typed_backup_retains_results_without_replaying_external_authority(env, tmp_path, monkeypatch):
    e = env
    identity, result = execute(e, reading(e), Step('evidence.read', {'topic': e.topic, 'evidence': e.evidence}))
    assert result.status == 'complete', result
    queued = e.service.submit(plan(e, Step('voice.list'), Step('audio.effects')))
    def fail(*args, **kwargs):
        pytest.fail('Passive backup/restore cannot activate a runtime or voice provider.')
    monkeypatch.setattr(runtime_settings, 'voice_registry', fail)
    monkeypatch.setattr(runtime_settings, 'audio_runtime', fail)
    directory = tmp_path / 'backup'
    checksum = e.app.backup(directory)
    shutil.rmtree(e.app.root)
    restored = LibraryApp(restore_backup(directory, tmp_path / 'restored', checksum))
    current = AssistantService(restored)
    assert current.store.load(identity)['steps'][0]['result']['text'] == result.results[0]['text']
    assert current.store.load(queued)['status'] == 'queued'
    for job in (identity, queued):
        with pytest.raises(PermissionError, match='another data directory'):
            current.resume(job)
    assert restored.assets.resolve(e.imported['asset']).read_bytes() == e.original.encode('utf-8')


def test_render_alias_uses_existing_production_callback_and_verifies_output(env):
    e = env
    calls = []
    def production(arguments, directory, cancelled, progress):
        calls.append(arguments)
        progress('existing production boundary')
        output = tone(directory / 'rendered.wav')
        return {'artifacts': [str(output)]}
    service = AssistantService(e.app, production=production)
    reviewed = Plan(e.scope, (Step('audio.render', {'text': 'Literal reviewed script',
                                                 'voice_ids': ['selected:exact']}),))
    identity = service.submit(reviewed)
    result = service.run(identity)
    assert result.status == 'complete' and len(calls) == 1
    assert calls[0]['text'] == 'Literal reviewed script'
    assert calls[0]['voice_ids'] == ['selected:exact'] and calls[0]['operation_id']
    assert service.resume(identity).results == result.results and len(calls) == 1
    (e.outputs / 'rendered.wav').write_bytes(b'Changed')
    assert service.resume(identity).status == 'failed' and len(calls) == 1


@pytest.mark.parametrize('changed', ['audio', 'manifest'])
@pytest.mark.parametrize('stop_after_publication', [False, True])
def test_conversion_cannot_adopt_replaced_publication_at_checkpoint(env, monkeypatch, changed, stop_after_publication):
    e = env
    from carr_library.audio import AudioRuntime
    from carr_library.durable_state import encode
    source = tone(e.inputs / 'reviewed.wav')
    original_source = source.read_bytes()
    stopped, calls, mutation = threading.Event(), [], {}
    convert = AudioRuntime.convert
    def counted(runtime, *args, **kwargs):
        calls.append(args[0])
        return convert(runtime, *args, **kwargs)
    monkeypatch.setattr(AudioRuntime, 'convert', counted)
    save = e.service.store.save
    def replace_after_receipt(record):
        result = save(record)
        checkpoint = record['steps'][0]
        if checkpoint['intent'].get('publication_committed') and not mutation:
            manifest_path = e.outputs / 'converted' / 'conversion-manifest.json'
            raw_manifest = manifest_path.read_bytes()
            producer = json.loads(raw_manifest)
            assert producer['status'] == 'completed' and producer['done'] == 1
            assert encode(producer) == raw_manifest
            candidate = producer['items'][0]['candidate']
            target = Path(candidate['path']) if changed == 'audio' else manifest_path
            expected = target.read_bytes()
            if changed == 'audio':
                assert candidate['size'] == len(expected)
                assert candidate['sha256'] == sha256(expected).hexdigest()
            # Same-sized replacement forces hash verification, not only a size
            # test. The producer completed real FFmpeg output before this swap.
            replacement = bytes([expected[0] ^ 1]) + expected[1:]
            staged = target.with_name(target.name + '.replacement')
            staged.write_bytes(replacement)
            os.replace(staged, target)
            mutation.update(path=target, original=expected, replacement=replacement,
                            producer=producer)
            if stop_after_publication:
                stopped.set()
        return result
    monkeypatch.setattr(e.service.store, 'save', replace_after_receipt)
    identity, result = execute(e, conversion(e, [source]), cancelled=stopped.is_set)
    assert mutation and len(calls) == 1
    assert result.status in ('failed', 'attention') and result.completed == 0
    assert mutation['path'].read_bytes() == mutation['replacement']
    record = e.service.store.load(identity)
    assert record['steps'][0]['intent']['publication_committed'] is True
    assert record['steps'][0]['status'] != 'complete'
    assert source.read_bytes() == original_source
    assert e.broker.snapshot()['leases'] == []
    def no_repeat(*args, **kwargs):
        pytest.fail('A published but unaccepted conversion cannot rerender or adopt an edited receipt.')
    monkeypatch.setattr(AudioRuntime, 'convert', no_repeat)
    stopped.clear()
    resumed = e.service.resume(identity)
    assert resumed.status == 'attention' and resumed.completed == 0
    assert mutation['path'].read_bytes() == mutation['replacement']
    assert source.read_bytes() == original_source


@pytest.mark.parametrize('format,suffix', [('markdown', 'md'), ('docx', 'docx')])
def test_evidence_export_cannot_adopt_replaced_publication_at_checkpoint(env, monkeypatch, format, suffix):
    e = env
    name = 'reviewed.' + suffix
    target = e.outputs / name
    mutation = {}
    save = e.service.store.save
    def replace_after_receipt(record):
        result = save(record)
        intent = record['steps'][0]['intent']
        if intent.get('publication_committed') and not mutation:
            expected = target.read_bytes()
            receipt = dict(intent['export_attempt'])
            assert receipt['size'] == len(expected)
            assert receipt['output_sha256'] == sha256(expected).hexdigest()
            replacement = bytes([expected[0] ^ 1]) + expected[1:]
            staged = target.with_name(target.name + '.replacement')
            staged.write_bytes(replacement)
            os.replace(staged, target)
            mutation.update(receipt=receipt, replacement=replacement)
        return result
    monkeypatch.setattr(e.service.store, 'save', replace_after_receipt)
    identity, result = execute(e, Step('evidence.export',
        {'topic': e.topic, 'evidence_ids': [e.evidence], 'name': name, 'format': format}))
    assert mutation and result.status in ('failed', 'attention') and result.completed == 0
    assert target.read_bytes() == mutation['replacement']
    saved = e.service.store.load(identity)['steps'][0]
    assert saved['intent']['publication_committed'] is True
    assert saved['intent']['export_attempt'] == mutation['receipt']
    def no_repeat(*args, **kwargs):
        pytest.fail('A published evidence export cannot be repeated after receipt rejection.')
    monkeypatch.setattr(export_file, 'write_export', no_repeat)
    monkeypatch.setattr(export_file, 'write_binary_export', no_repeat)
    assert e.service.resume(identity).status == 'attention'
    assert target.read_bytes() == mutation['replacement']
    assert e.app.evidence(e.workspace, e.topic)[0].evidence_id == e.evidence


@pytest.mark.parametrize('declared', [True, False])
def test_artifact_verification_refuses_oversized_file_before_open(env, monkeypatch, declared):
    e = env
    from carr_library.audio import conversion_batch
    target = e.outputs / 'oversized.bin'
    limit = e.scope.max_bytes
    extent = 128 if declared else limit + 1
    with target.open('wb') as stream:
        stream.truncate(extent)
    expected = {'path': str(target), 'size': 4, 'sha256': sha256(b'good').hexdigest()}
    def forbidden(*args, **kwargs):
        pytest.fail('An oversized or receipt-size-mismatched artifact cannot be opened for hashing.')
    monkeypatch.setattr(conversion_batch, '_open_json_read', forbidden)
    # Guard the previous unbounded implementation too, so baseline failure is
    # observable even when it does not use the canonical descriptor reader.
    monkeypatch.setattr(Path, 'open', forbidden)
    with pytest.raises(ValueError, match='receipt|budget'):
        e.service._artifacts({'artifacts': [expected if declared else str(target)]}, e.scope, lambda: False)
    assert target.stat().st_size == extent


def test_artifact_hashing_retains_descriptor_identity_and_a_fixed_growth_bound(env, monkeypatch):
    e = env
    from carr_library.audio import conversion_batch
    target = e.outputs / 'growing.bin'
    expected_raw = b'receipt'
    target.write_bytes(expected_raw)
    expected = {'path': str(target), 'size': len(expected_raw),
                'sha256': sha256(expected_raw).hexdigest()}
    original_open, original_read = Path.open, conversion_batch._open_json_read
    reads, grown, opening_reader = [], False, False
    class Observed:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def fileno(self):
            return self.stream.fileno()
        def read(self, size=-1):
            nonlocal grown
            assert size >= 0, 'Artifact verification must use bounded reads.'
            if not grown:
                grown = True
                with original_open(target, 'ab') as writer:
                    writer.write(b'x' * 4096)
            data = self.stream.read(size)
            reads.append(len(data))
            return data
    def observe(path):
        nonlocal opening_reader
        opening_reader = True
        try:
            return Observed(original_read(path))
        finally:
            opening_reader = False
    monkeypatch.setattr(conversion_batch, '_open_json_read', observe)
    # Legacy baseline opens via Path.open, which must face the same real growth.
    def path_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return Observed(stream) if path == target and args and args[0] == 'rb' and not opening_reader else stream
    monkeypatch.setattr(Path, 'open', path_open)
    with pytest.raises(ValueError, match='grew|changed|receipt'):
        e.service._artifacts({'artifacts': [expected]}, e.scope, lambda: False)
    assert grown and 0 < sum(reads) <= len(expected_raw) + 1
    assert target.read_bytes() == expected_raw + b'x' * 4096


@pytest.mark.parametrize('change', ['hash', 'size', 'missing_hash', 'boolean_size'])
def test_declared_production_receipt_cannot_be_replaced_with_current_file_identity(env, change):
    e = env
    target = tone(e.outputs / 'producer.wav')
    raw = target.read_bytes()
    receipt = {'path': str(target), 'size': len(raw), 'sha256': sha256(raw).hexdigest()}
    if change == 'hash':
        receipt['sha256'] = '0' * 64
    elif change == 'size':
        receipt['size'] -= 1
    elif change == 'missing_hash':
        del receipt['sha256']
    else:
        receipt['size'] = True
    calls = []
    def production(*args):
        calls.append(True)
        return {'status': 'complete', 'artifacts': [receipt]}
    service = AssistantService(e.app, production=production)
    identity = service.submit(plan(e, Step('audio.render',
        {'text': 'Exact source', 'voice_ids': ['selected:exact']})))
    result = service.run(identity)
    assert calls == [True] and result.status == 'failed' and result.completed == 0
    assert target.read_bytes() == raw


@pytest.mark.parametrize('producer_status,assistant_status', [
    ('paused', 'paused'), ('cancelled', 'paused'), ('failed', 'failed'),
])
def test_partial_production_artifacts_do_not_complete_the_assistant_step(env, producer_status, assistant_status):
    e = env
    target = tone(e.outputs / 'partial.wav')
    raw = target.read_bytes()
    receipt = {'path': str(target), 'size': len(raw), 'sha256': sha256(raw).hexdigest()}
    def production(*args):
        return {'job_id': 'existing-production', 'status': producer_status, 'artifacts': [receipt]}
    service = AssistantService(e.app, production=production)
    identity = service.submit(plan(e, Step('audio.render',
        {'text': 'Reviewed production', 'voice_ids': ['selected:exact']})))
    result = service.run(identity)
    assert result.status == assistant_status and result.completed == 0
    assert result.results == ()
    assert target.read_bytes() == raw
