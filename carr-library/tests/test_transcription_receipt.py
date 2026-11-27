"""Real synthetic files for a neutral receipt verifier; no speech engine is run."""
import hashlib
import json
import os
import traceback

import pytest

from carr_library.audio.models import canonical, digest
from carr_library import transcription_receipt as receipt


def sha(data):
    return hashlib.sha256(data).hexdigest()


class SyntheticJob:
    def __init__(self, root, *, mode='separate', formats=('txt', 'srt', 'vtt', 'json')):
        self.job = root / 'transcription-jobs' / ('a' * 32)
        self.job.mkdir(parents=True)
        self.config_path = self.job / 'job_config.json'
        self.receipt_path = self.job / 'logs/completion-v1.json'
        self.receipt_path.parent.mkdir()
        self.result_root = self.job / 'results' / 'worker-child'
        self.result_root.mkdir(parents=True)
        self.runtime = root / 'runtime-manifest.json'
        self.runtime.write_bytes(b'{"synthetic_runtime_identity":true}\n')
        self.runtime_sha = sha(self.runtime.read_bytes())
        self.model_sha = digest({'config.json': 'b' * 64, 'model.bin': 'c' * 64,
                                 'tokenizer.json': 'd' * 64})
        self.folders = [self.job / 'inputs/root-0', self.job / 'inputs/root-1']
        self.input_paths = []
        inputs = []
        for index, (folder, name) in enumerate(((self.folders[0], 'Україна.wav'),
                (self.folders[0], 'nested/Slovensko.wav'), (self.folders[1], 'other.wav'))):
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            data = ('Synthetic captured input ' + str(index) + ' — Україна.').encode('utf-8')
            path.write_bytes(data)
            self.input_paths.append(path)
            inputs.append(dict(path=str(path), root=str(folder), relative=name,
                               bytes=len(data), sha256=sha(data)))
        self.settings = receipt.validate_settings(dict(output_mode=mode,
            **{'output_' + name: name in formats for name in ('txt', 'srt', 'vtt', 'json')}))
        self.config = dict(run_id='a' * 32, runtime_manifest=str(self.runtime),
            runtime_manifest_sha256=self.runtime_sha, settings=dict(self.settings),
            models_dir=str(root / 'unopened-model-directory'),
            folders=[str(path) for path in self.folders], results_root=str(self.job / 'results'),
            inputs=inputs, job_name='Synthetic public fixture',
            source_provenance=[dict(index=0, path=str(root / 'original-may-now-be-unavailable.wav'))])
        self.payload = dict(run_id=self.config['run_id'], job_root=str(self.job),
            job_config_sha256='', results_root=str(self.result_root),
            output_settings={key: self.settings[key] for key in receipt.OUTPUT_SETTINGS_KEYS},
            inputs=[dict(row, effective_settings=dict(self.settings), model_sha256=self.model_sha,
                         runtime_sha256=self.runtime_sha) for row in inputs], outputs=[])
        self.output_paths = []
        for format in formats:
            groups = [[0], [1], [2]]
            if format == 'txt' and mode == 'one_per_folder':
                groups = [[0, 1], [2]]
            elif format == 'txt' and mode == 'one_whole_job':
                groups = [[0, 1, 2]]
            for index, group in enumerate(groups):
                relative = f'{format}/result-{index}.{format}'
                target = self.result_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                data = ('Synthetic derivative — ' + format + ' ' + str(group)).encode('utf-8')
                target.write_bytes(data)
                self.output_paths.append(target)
                self.payload['outputs'].append(dict(path=relative, format=format,
                    source_indexes=group, bytes=len(data), sha256=sha(data)))
        self.status = dict(protocol=receipt.PROTOCOL, run_id=self.config['run_id'],
                           state='завершено', done=3, failed=0, total=3)
        self.write_config()
        self.seal()

    def write_config(self):
        self.config_path.write_bytes((canonical(self.config) + '\n').encode('utf-8'))
        self.config_sha = sha(self.config_path.read_bytes())
        self.payload['job_config_sha256'] = self.config_sha

    def publish_bytes(self, data):
        self.receipt_path.write_bytes(data)
        self.status['completion_sha256'] = sha(data)

    def seal(self):
        envelope = dict(schema=receipt.PROTOCOL, payload=self.payload, sha256=digest(self.payload))
        self.publish_bytes((canonical(envelope) + '\n').encode('utf-8'))

    def verify(self, **kwargs):
        return receipt.verify_completion(self.job, self.config, self.status,
            expected_config_sha256=self.config_sha, expected_runtime_sha256=self.runtime_sha,
            expected_model_sha256=self.model_sha, **kwargs)


@pytest.fixture
def job(tmp_path):
    return SyntheticJob(tmp_path)


@pytest.mark.parametrize('mode,txt_count', [('separate', 3), ('one_per_folder', 2), ('one_whole_job', 1)])
def test_exact_formats_groups_bytes_and_unicode_are_verified_without_engine_execution(tmp_path, mode, txt_count):
    job = SyntheticJob(tmp_path, mode=mode)
    before = {path: path.read_bytes() for path in (job.config_path, job.receipt_path,
              job.runtime, *job.input_paths, *job.output_paths)}
    result = job.verify()
    assert result['protocol'] == receipt.PROTOCOL and result['run_id'] == 'a' * 32
    assert result['input_count'] == 3 and result['output_count'] == txt_count + 9
    assert result['results_root'] == str(job.result_root)
    assert result['completion_sha256'] == sha(job.receipt_path.read_bytes())
    assert result['job_config_sha256'] == job.config_sha
    assert result['runtime_manifest_sha256'] == job.runtime_sha
    assert result['model_sha256'] == job.model_sha
    assert result['outputs'] == job.payload['outputs']
    assert {path: path.read_bytes() for path in before} == before
    assert 'source_provenance' not in result and 'inputs' not in result
    json.dumps(result, ensure_ascii=False, allow_nan=False)


def test_subtitles_remain_per_input_without_txt_even_in_whole_job_mode(tmp_path):
    job = SyntheticJob(tmp_path, mode='one_whole_job', formats=('srt', 'vtt', 'json'))
    result = job.verify()
    assert result['output_count'] == 9
    assert all(len(row['source_indexes']) == 1 for row in result['outputs'])


@pytest.mark.parametrize('state', ['completed', 'завершено'])
def test_both_completed_status_spellings_require_the_same_receipt(job, state):
    job.status['state'] = state
    assert job.verify()['output_count'] == 12
    job.receipt_path.unlink()
    with pytest.raises(receipt.TranscriptionReceiptError):
        job.verify()


@pytest.mark.parametrize('field,value', [('state', 'running'), ('state', 'failed'),
    ('protocol', 'legacy'), ('run_id', 'b' * 32), ('completion_sha256', '0' * 64),
    ('completion_sha256', True)])
def test_status_without_correlated_hardened_completion_is_not_success(job, field, value):
    job.status[field] = value
    with pytest.raises(receipt.TranscriptionReceiptError):
        job.verify()


def test_receipt_raw_bytes_not_only_payload_digest_are_bound_to_status(job):
    job.receipt_path.write_bytes(job.receipt_path.read_bytes() + b'\n')
    with pytest.raises(receipt.TranscriptionReceiptError, match='final status'):
        job.verify()


def test_payload_digest_is_independently_required_even_when_status_hash_matches(job):
    envelope = json.loads(job.receipt_path.read_bytes())
    envelope['sha256'] = '0' * 64
    job.publish_bytes(canonical(envelope).encode('utf-8'))
    with pytest.raises(receipt.TranscriptionReceiptError, match='payload'):
        job.verify()


@pytest.mark.parametrize('where', ['actual-bytes', 'caller-object'])
def test_actual_configuration_and_caller_snapshot_are_both_bound(job, where):
    if where == 'actual-bytes':
        job.config_path.write_bytes(job.config_path.read_bytes() + b' ')
    else:
        job.config['settings']['beam_size'] = 2
    with pytest.raises(receipt.TranscriptionReceiptError, match='configuration differs'):
        job.verify()


@pytest.mark.parametrize('kind', ['runtime', 'input', 'output'])
def test_changed_actual_bytes_are_rejected_even_with_unchanged_size(job, kind):
    path = {'runtime': job.runtime, 'input': job.input_paths[0], 'output': job.output_paths[0]}[kind]
    path.write_bytes(b'X' * path.stat().st_size)
    with pytest.raises(receipt.TranscriptionReceiptError):
        job.verify()


def test_silent_empty_text_still_requires_an_actual_bound_output_file(tmp_path):
    job = SyntheticJob(tmp_path, formats=('txt',))
    job.output_paths[0].write_bytes(b'')
    job.payload['outputs'][0].update(bytes=0, sha256=sha(b''))
    job.seal()
    assert job.verify()['outputs'][0]['bytes'] == 0
    job.output_paths[0].unlink()
    with pytest.raises(receipt.TranscriptionReceiptError):
        job.verify()


@pytest.mark.parametrize('kind', ['config', 'receipt', 'runtime', 'input', 'output'])
def test_real_hardlinks_are_not_admitted_as_owned_job_files(job, tmp_path, kind):
    path = {'config': job.config_path, 'receipt': job.receipt_path, 'runtime': job.runtime,
            'input': job.input_paths[0], 'output': job.output_paths[0]}[kind]
    os.link(path, tmp_path / ('alias-' + kind))
    with pytest.raises(receipt.TranscriptionReceiptError, match='without links'):
        job.verify()


def test_same_output_path_cannot_cover_two_distinct_inputs(job):
    first, second = job.payload['outputs'][:2]
    second.update(path=first['path'], bytes=first['bytes'], sha256=first['sha256'])
    job.seal()
    with pytest.raises(receipt.TranscriptionReceiptError, match='duplicate'):
        job.verify()


@pytest.mark.parametrize('name', ['../outside.txt', '/outside.txt', 'bad\\name.txt',
    'one//two.txt', 'one/./two.txt', 'drive:stream.txt', 'CON.txt', 'trailing.txt.'])
def test_output_names_cannot_escape_or_use_windows_aliases(job, name):
    job.payload['outputs'][0]['path'] = name
    job.seal()
    with pytest.raises(receipt.TranscriptionReceiptError):
        job.verify()


@pytest.mark.parametrize('indices', [[], [True], [-1], [3], [1, 0], [0, 0]],
                         ids=['empty', 'boolean', 'negative', 'outside', 'unordered', 'duplicate'])
def test_output_indices_are_strict_sorted_unique_positions(job, indices):
    job.payload['outputs'][0]['source_indexes'] = indices
    job.seal()
    with pytest.raises(receipt.TranscriptionReceiptError, match='indices'):
        job.verify()


@pytest.mark.parametrize('change', ['missing', 'duplicate-index', 'merged-subtitles', 'wrong-format'])
def test_receipt_requires_exact_requested_format_and_source_coverage(job, change):
    if change == 'missing':
        job.payload['outputs'].pop()
    elif change == 'duplicate-index':
        job.payload['outputs'][1]['source_indexes'] = [0]
    elif change == 'merged-subtitles':
        next(row for row in job.payload['outputs'] if row['format'] == 'srt')['source_indexes'] = [0, 1]
    else:
        job.payload['outputs'][0]['format'] = 'vtt'
    job.seal()
    with pytest.raises(receipt.TranscriptionReceiptError):
        job.verify()


@pytest.mark.parametrize('field,value', [('model_sha256', 'e' * 64), ('runtime_sha256', 'f' * 64),
                                       ('path', '/unselected-source.wav'), ('bytes', True)])
def test_each_receipt_input_is_bound_to_the_original_request_and_engine(job, field, value):
    job.payload['inputs'][0][field] = value
    job.seal()
    with pytest.raises(receipt.TranscriptionReceiptError, match='receipt input'):
        job.verify()


@pytest.mark.parametrize('change', ['live-setting', 'missing-setting', 'output-setting'])
def test_undeclared_live_or_export_setting_changes_require_attention(job, change):
    if change == 'live-setting':
        job.payload['inputs'][0]['effective_settings']['beam_size'] = 2
    elif change == 'missing-setting':
        del job.payload['inputs'][0]['effective_settings']['parallel_files']
    else:
        job.payload['output_settings']['include_headers'] = True
    job.seal()
    with pytest.raises(receipt.TranscriptionReceiptError):
        job.verify()


@pytest.mark.parametrize('where', ['payload', 'input', 'output', 'output-settings'])
def test_receipt_records_reject_extra_fields(job, where):
    selected = {'payload': job.payload, 'input': job.payload['inputs'][0],
                'output': job.payload['outputs'][0], 'output-settings': job.payload['output_settings']}[where]
    selected['undeclared'] = 'not admitted'
    job.seal()
    with pytest.raises(receipt.TranscriptionReceiptError, match='fields'):
        job.verify()


@pytest.mark.parametrize('field', ['job_root', 'results_root'])
def test_receipt_cannot_rebind_job_or_results_to_another_directory(job, tmp_path, field):
    other = tmp_path / 'different-scope'
    other.mkdir()
    job.payload[field] = str(other)
    job.seal()
    with pytest.raises(receipt.TranscriptionReceiptError):
        job.verify()


@pytest.mark.parametrize('inventory', ['inputs', 'outputs'])
def test_receipt_inventory_limits_are_checked_before_unbounded_file_verification(job, inventory):
    if inventory == 'inputs':
        job.config['inputs'] = [dict(job.config['inputs'][0])] * (receipt.MAX_INPUTS + 1)
        job.write_config()
    else:
        job.payload['outputs'] = [dict(job.payload['outputs'][0])] * (receipt.MAX_OUTPUTS + 1)
    job.seal()
    with pytest.raises(receipt.TranscriptionReceiptError, match='inventory'):
        job.verify()


def test_oversized_receipt_is_rejected_by_existing_bounded_reader(job):
    job.publish_bytes(b' ' * (receipt.MAX_RECEIPT_BYTES + 1))
    with pytest.raises(receipt.TranscriptionReceiptError, match='bounded'):
        job.verify()


@pytest.mark.parametrize('data', [b'{"schema":1,"schema":2}', b'{"number":NaN}', b'\xff'])
def test_receipt_json_is_unambiguous_finite_utf8(job, data):
    job.publish_bytes(data)
    with pytest.raises(receipt.TranscriptionReceiptError, match='JSON'):
        job.verify()


def test_cancel_is_honored_before_reading_any_job_file(job):
    with pytest.raises(InterruptedError, match='cancelled'):
        job.verify(cancelled=lambda: True)


def test_cancel_during_input_hash_stops_validation(job, monkeypatch):
    original = receipt.digest_file
    stopping = False
    def interrupted(path, **kwargs):
        nonlocal stopping
        if path == job.input_paths[0]:
            stopping = True
        return original(path, **kwargs)
    monkeypatch.setattr(receipt, 'digest_file', interrupted)
    with pytest.raises(InterruptedError):
        job.verify(cancelled=lambda: stopping)


def test_earlier_input_changed_while_last_output_is_hashed_is_detected(job, monkeypatch):
    original = receipt.digest_file
    def later_change(path, **kwargs):
        value = original(path, **kwargs)
        if path == job.output_paths[-1]:
            job.input_paths[0].write_bytes(b'Another writer changed this earlier source.')
        return value
    monkeypatch.setattr(receipt, 'digest_file', later_change)
    with pytest.raises(receipt.TranscriptionReceiptError, match='changed during'):
        job.verify()


def test_input_paths_are_not_leaked_by_errors_or_tracebacks(job, tmp_path):
    private_path = tmp_path / 'do-not-disclose-original-personal-name.wav'
    job.config['inputs'][0]['path'] = str(private_path)
    job.write_config()
    job.seal()
    with pytest.raises(receipt.TranscriptionReceiptError) as failure:
        job.verify()
    formatted = ''.join(traceback.format_exception(failure.value))
    assert str(private_path) not in formatted
    assert private_path.name not in str(failure.value)


def test_settings_defaults_are_complete_independent_and_translation_is_explicit():
    first = receipt.validate_settings({})
    assert len(first) == 17 and first['output_txt'] is True and first['vad_filter'] is True
    first['output_txt'] = False
    assert receipt.validate_settings({})['output_txt'] is True
    translated = receipt.validate_settings(dict(task='translate', language='uk', translation_target='en'))
    assert translated['task'] == 'translate' and translated['language'] == 'uk'
    with pytest.raises(TypeError):
        receipt.DEFAULT_SETTINGS['model'] = 'changed'


@pytest.mark.parametrize('patch', [dict(unknown=True), dict(beam_size=True), dict(beam_size=11),
    dict(parallel_files=0), dict(parallel_files=21), dict(output_txt=1), dict(vad_filter='yes'),
    dict(task='summarize'), dict(translation_target='uk'), dict(language='UK'), dict(language='zz'),
    dict(model='../remote'), dict(model='CON'), dict(model='name:stream'), dict(device='network'),
    dict(compute_type='pickle'), dict(output_txt=False)],
    ids=['unknown', 'bool-int', 'beam-limit', 'parallel-zero', 'parallel-limit', 'bool-output',
         'bool-vad', 'task', 'translation-target', 'language-case', 'unknown-language',
         'model-traversal', 'model-device', 'model-stream', 'device', 'compute', 'no-output'])
def test_settings_reject_undeclared_types_ranges_or_authority(patch):
    with pytest.raises(receipt.TranscriptionReceiptError):
        receipt.validate_settings(patch)


def test_pure_integrity_record_retains_historical_paths_without_opening_them(monkeypatch):
    payload = {'job_root': '/historical-unavailable/Україна', 'values': [1, True, None]}
    envelope = {'schema': 'supplied-whisper-v43-integrity/1/context',
                'payload': payload, 'sha256': receipt.canonical_digest(payload)}
    def no_files(*args, **kwargs):
        raise AssertionError('Pure integrity validation must not access files.')
    monkeypatch.setattr(receipt, 'local_path', no_files)
    assert receipt.validate_record(envelope, 'context') == payload
    assert receipt.canonical_digest(payload) == digest(payload)


@pytest.mark.parametrize('change', ['extra', 'wrong-kind', 'wrong-hash', 'payload-list',
                                    'bad-kind', 'non-finite', 'too-large'])
def test_pure_integrity_record_requires_exact_schema_payload_digest_and_budget(change):
    payload = {'value': 'Україна'}
    envelope = {'schema': 'supplied-whisper-v43-integrity/1/context',
                'payload': payload, 'sha256': digest(payload)}
    kind, maximum = 'context', 64 * 1024 * 1024
    if change == 'extra':
        envelope['unrecognized'] = True
    elif change == 'wrong-kind':
        envelope['schema'] = 'supplied-whisper-v43-integrity/1/owner'
    elif change == 'wrong-hash':
        envelope['sha256'] = '0' * 64
    elif change == 'payload-list':
        envelope['payload'] = []
    elif change == 'bad-kind':
        kind = '../context'
    elif change == 'non-finite':
        envelope['payload']['value'] = float('nan')
    else:
        maximum = 2
    with pytest.raises(receipt.TranscriptionReceiptError):
        receipt.validate_record(envelope, kind, maximum=maximum)


def test_integrity_record_default_budget_is_distinct_from_completion_file_budget():
    # No large allocation is needed to establish the explicit public contract.
    import inspect
    assert inspect.signature(receipt.validate_record).parameters['maximum'].default == 64 * 1024 * 1024
    assert receipt.MAX_RECEIPT_BYTES == 16 * 1024 * 1024


@pytest.mark.parametrize('model', ['two names', 'модель', '_hidden', '-small', 'x' * 65],
                         ids=['space', 'unicode', 'leading-underscore', 'leading-hyphen', 'length'])
def test_model_directory_matches_the_reviewed_worker_selector(model):
    with pytest.raises(receipt.TranscriptionReceiptError):
        receipt.validate_settings({'model': model})


@pytest.mark.parametrize('model', ['small', 'large-v3', 'distil.medium.en', 'Model_2', 'x' * 64])
def test_reviewed_worker_model_directory_syntax_is_retained(model):
    assert receipt.validate_settings({'model': model})['model'] == model


def test_readonly_reopening_can_verify_outputs_after_external_runtime_removal(job):
    job.runtime.unlink()
    with pytest.raises(receipt.TranscriptionReceiptError):
        job.verify()
    result = job.verify(verify_runtime_current=False)
    assert result['runtime_current_verified'] is False
    assert result['runtime_manifest_sha256'] == job.runtime_sha
    assert result['model_sha256'] == job.model_sha and result['output_count'] == 12


@pytest.mark.parametrize('target', ['configuration', 'input', 'output', 'receipt'])
def test_readonly_mode_preserves_all_local_job_byte_checks(job, target):
    job.runtime.unlink()
    path = {'configuration': job.config_path, 'input': job.input_paths[0],
            'output': job.output_paths[0], 'receipt': job.receipt_path}[target]
    path.write_bytes(path.read_bytes() + b'changed')
    with pytest.raises(receipt.TranscriptionReceiptError):
        job.verify(verify_runtime_current=False)


@pytest.mark.parametrize('field', ['runtime_sha256', 'model_sha256'])
def test_readonly_mode_still_requires_original_runtime_and_model_bindings(job, field):
    job.runtime.unlink()
    job.payload['inputs'][0][field] = '0' * 64
    job.seal()
    with pytest.raises(receipt.TranscriptionReceiptError):
        job.verify(verify_runtime_current=False)


def test_current_runtime_check_cannot_be_disabled_by_nonboolean_option(job):
    with pytest.raises(receipt.TranscriptionReceiptError, match='boolean'):
        job.verify(verify_runtime_current=0)
    assert job.verify()['runtime_current_verified'] is True
