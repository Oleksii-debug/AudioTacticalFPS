"""Synthetic canonical data only; no inference, executable runtime or weights."""
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

from carr_library.legacy_transcription_values import encode, fingerprint, settings, encoded_outputs


def digest(raw):
    return sha256(raw).hexdigest()


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = encode(value)
    path.write_bytes(raw)
    return raw


def canonical_data(root, *, complete=False, empty=False):
    root = Path(root)
    namespace = root / 'state/transcription'
    model_files = {name: dict(size=4, sha256=digest(b'TEST')) for name in
                   ('model.bin', 'config.json', 'tokenizer.json')}
    profile = dict(schema='resonance-transcription-runtime/1', protocol='local-file-job/1',
        python_executable='C:/absent-private-runtime/python.exe', python_resolved='C:/absent-private-runtime/python.exe',
        python_identity=dict(size=4, sha256=digest(b'TEST')), python_configuration={},
        runtime_distributions={name: dict(version='0.synthetic', file_count=1,
            installed_files_sha256='1' * 64) for name in
            ('faster-whisper', 'ctranslate2', 'av', 'onnxruntime', 'numpy', 'huggingface-hub', 'tokenizers')},
        runtime_auxiliary={}, runtime_capability='installed-files-reviewed; execution-and-quality-unqualified',
        worker_path='C:/absent-private-runtime/worker.py',
        worker_identity=dict(size=4, sha256=digest(b'TEST')),
        model_directory='C:/absent-private-model/small', model_name='small',
        model_files=model_files, model_bytes=12, model_content_fingerprint=fingerprint(model_files))
    profile['fingerprint'] = fingerprint(profile)
    profile.update(id=profile['fingerprint'][:32], name='Unqualified synthetic historical runtime',
                   trusted=True, qualification='unqualified', registered=1.0, restored=False)
    profile_path = namespace / 'profiles' / (profile['id'] + '.json')
    profile_raw = put(profile_path, profile)
    identity, source_id, result_id, edit_id = '1' * 32, '2' * 32, '3' * 32, '4' * 32
    job_root = namespace / 'jobs' / identity
    raw = b'Original retained synthetic audio; never decoded.'
    retained = 'inputs/' + source_id + '/source.wav'
    source = dict(id=source_id, original_path='C:/lost-original/Запис.wav', name='Запис.wav',
                  retained_path=retained, size=len(raw), sha256=digest(raw))
    target = job_root / retained
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    options = settings(dict(output_srt=True, output_vtt=True))
    job = dict(schema='resonance-transcription-job/1', id=identity, title='Історичний запис',
        status='pending', profile_id=profile['id'], profile_fingerprint=profile['fingerprint'],
        sources=[source], settings=options, results=[], edits={}, done=0, total=1,
        error='', created=1.0, updated=1.0, recovery_review_required=False, restored=False)
    text = '' if empty else 'Історичний синтетичний результат.'
    result = dict(language='uk', duration=1.0, segments=[] if empty else [
        dict(start=0.0, end=1.0, text=text, words=[])], text=text)
    provenance = dict(citation_authority=False, derived=True, diarization=False,
        effective_settings={**options, 'model': 'small', 'translation_target': 'en'},
        model_content_fingerprint=profile['model_content_fingerprint'],
        profile_fingerprint=profile['fingerprint'], profile_id=profile['id'],
        python_sha256=profile['python_identity']['sha256'], quality='unqualified',
        runtime_identity_sha256='5' * 64, settings=options, source_bytes=len(raw),
        source_sha256=digest(raw), translation_target=None, worker_checkpoint_sha256='6' * 64,
        worker_sha256=profile['worker_identity']['sha256'])
    if complete:
        output = {}
        for extension, data in encoded_outputs(result, provenance).items():
            relative = f'artifacts/{source_id}/{result_id}/transcript.{extension}'
            path = job_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            output[extension] = dict(path=relative, sha256=digest(data), size=len(data))
        receipt = dict(schema='resonance-transcription-result/1', source_id=source_id,
            result=result, provenance=provenance, files=output, created=2.0, status='complete')
        relative_receipt = f'artifacts/{source_id}/{result_id}/receipt.json'
        receipt_raw = put(job_root / relative_receipt, receipt)
        job['results'] = [dict(source_id=source_id, receipt=relative_receipt, sha256=digest(receipt_raw),
            formats=list(output), language='uk', duration=1.0, segments=len(result['segments']))]
        edited = dict(id=edit_id, source_id=source_id, text='Збережена окрема правка.',
            edited=True, provenance={**provenance, 'revision_kind': 'user-edited-transcript',
                'original_transcript_sha256':digest(text.encode('utf-8'))},
            created=3.0, path=f'edits/{source_id}/{edit_id}.json')
        edited_raw = put(job_root / edited['path'], edited)
        job['edits'][source_id] = dict(id=edit_id, path=edited['path'], sha256=digest(edited_raw))
        job['status'], job['done'] = 'complete', 1
    job['plan_fingerprint'] = fingerprint({name: job[name] for name in
        ('id', 'profile_id', 'profile_fingerprint', 'sources', 'settings')})
    put(job_root / 'job.json', job)
    put(job_root / 'control.json', dict(action='run'))
    (job_root / 'worker/logs').mkdir(parents=True)
    (job_root / 'worker/pause.flag').write_bytes(b'')
    (job_root / 'worker/logs/progress.log').write_bytes(b'')
    return SimpleNamespace(root=root, namespace=namespace, job_root=job_root, job=job,
        profile=profile, profile_path=profile_path, profile_raw=profile_raw, source=source,
        source_path=target, raw=raw, text=text)


def current_runtime(directory):
    from carr_library.transcription_receipt import PROTOCOL
    directory = Path(directory)
    assets = {'python-fixture': b'Nonexecutable synthetic inventory; no launch permitted.',
        'worker.py': b'raise SystemExit("This fixture must never launch")\n',
        'models/small/model.bin': b'NOT MODEL WEIGHTS', 'models/small/config.json': b'{}',
        'models/small/tokenizer.json': b'{}'}
    for name, raw in assets.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    value = dict(schema_version=1, engine='supplied-whisper', model_id='synthetic-recovery',
        revision='a' * 40, license='TEST FIXTURE', license_accepted=True, trusted_weights=True,
        files={name: digest(raw) for name, raw in assets.items()},
        settings=dict(protocol=PROTOCOL, interpreter='python-fixture', worker='worker.py',
            models_dir='models', model='small', languages=['uk', 'en'],
            devices=['cpu'], compute_types=['int8'], tasks=['transcribe', 'translate']))
    path = directory / 'manifest.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    return path
