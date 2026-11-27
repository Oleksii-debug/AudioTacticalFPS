"""Actual CLI subprocesses share private jobs; synthetic admission is test-only."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import threading
from types import SimpleNamespace

import pytest

import carr_library.transcription as transcription
from carr_library.durable_state import read_json
import carr_library.transcription_inputs as input_selection
from carr_library.transcription_inputs import select_inputs

_fixture_spec = importlib.util.spec_from_file_location('transcription_cli_fixtures', Path(__file__).with_name('test_transcription.py'))
_fixtures = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixtures)
engine = _fixtures.engine
source = _fixtures.source


def command(arguments, *, admitted=None, interrupt_trigger=None):
    environment = dict(os.environ)
    import bootstrap as selected_bootstrap
    package_roots = dict.fromkeys(str(Path(module.__file__).resolve().parents[1])
                                 for module in (transcription, selected_bootstrap))
    environment['PYTHONPATH'] = os.pathsep.join(package_roots)
    environment['PYTHONUTF8'] = '1'
    if admitted is None and interrupt_trigger is None:
        return [sys.executable, '-B', '-m', 'carr_library', *arguments], environment
    bootstrap = 'import runpy\nimport carr_library.transcription as module\n'
    if admitted is not None:
        bootstrap += 'module.REVIEWED_WORKER_SHA256 = module.REVIEWED_WORKER_SHA256 | {' + repr(admitted) + '}\n'
    if interrupt_trigger is not None:
        bootstrap += ('import threading,time,_thread\nfrom pathlib import Path\n'
            'def interrupt_when_requested():\n'
            '    while not Path(' + repr(str(interrupt_trigger)) + ').exists(): time.sleep(.02)\n'
            '    _thread.interrupt_main()\n'
            'threading.Thread(target=interrupt_when_requested,daemon=True).start()\n')
    bootstrap += 'runpy.run_module("carr_library", run_name="__main__")\n'
    return [sys.executable, '-B', '-c', bootstrap, *arguments], environment


def cli(root, *arguments, admitted=None, timeout=15):
    cmd, environment = command(['transcription', '--data-dir', str(root), *map(str, arguments)], admitted=admitted)
    return subprocess.run(cmd, env=environment, cwd=root.parent, text=True, encoding='utf-8', capture_output=True, timeout=timeout)


def payload(result, expected=0):
    assert result.returncode == expected, result.stdout + '\n' + result.stderr
    return json.loads(result.stdout)


def configure(root, spec, *, admitted=True):
    return cli(root, 'configure', '--python', spec.python_path, '--worker', spec.worker_path,
               '--worker-sha256', spec.worker_sha256, '--models-dir', spec.models_dir,
               admitted=spec.worker_sha256 if admitted else None)


def test_help_and_invalid_arguments_do_not_create_data(tmp_path):
    root = tmp_path / 'unused'
    result = cli(root, '--help')
    assert result.returncode == 0 and 'submit and execution are separate' in result.stdout
    assert not root.exists()
    for args in [('run','../bad'), ('jobs','--self-test'), ('capability','studio'),
                 ('submit','audio.wav','--run'), ('configure','--python','missing'),
                 ('run','0'*32,'--timeout','inf'), ('run','0'*32,'--stop-grace','0'),
                 ('edit','0'*32,'--artifact','../outside.txt','--text-file','missing'),
                 ('submit',), ('submit','audio.wav','--recursive')]:
        result = cli(root,*args)
        assert result.returncode == 2, result.stderr
        assert not root.exists(), args


def folder_sources(tmp_path):
    folder = tmp_path / 'selected-audio'
    folder.mkdir()
    (folder / 'b.wav').write_bytes(b'RIFF second synthetic input')
    (folder / 'A.WAV').write_bytes(b'RIFF first synthetic input')
    (folder / 'notes.txt').write_text('Not an audio input.', encoding='utf-8')
    return folder


def test_folder_selection_is_sorted_finite_and_recursion_is_explicit(tmp_path):
    folder = folder_sources(tmp_path)
    nested = folder / 'subfolder'; nested.mkdir()
    (nested / 'c.wav').write_bytes(b'RIFF nested synthetic input')
    selected = select_inputs(folders=(folder,))
    assert [path.name for path in selected.files] == ['A.WAV', 'b.wav']
    assert selected.skipped_count == 2
    assert 'notes.txt' in selected.describe() and 'Subfolder excluded' in selected.describe()
    expanded = select_inputs(folders=(folder,), recursive=True)
    assert [path.name for path in expanded.files] == ['A.WAV', 'b.wav', 'c.wav']
    assert expanded.skipped_count == 1
    (folder / 'later.wav').write_bytes(b'RIFF added after selection')
    assert len(selected.files) == 2  # a review is not a live folder watch


def test_explicit_file_and_overlapping_folders_are_included_once(tmp_path):
    folder = folder_sources(tmp_path)
    selected = select_inputs((folder / 'b.wav',), folders=(folder, folder))
    assert [path.name for path in selected.files] == ['b.wav', 'A.WAV']
    assert selected.skipped_count == 2 and 'included only once' in selected.describe()


@pytest.mark.parametrize('limit', ['files', 'entries', 'file-bytes', 'job-bytes', 'depth'])
def test_folder_scan_fails_closed_at_count_byte_and_depth_budgets(tmp_path, monkeypatch, limit):
    folder = folder_sources(tmp_path)
    selected_limit = {'files': 'MAX_INPUT_FILES', 'entries': 'MAX_SCAN_ENTRIES',
                      'file-bytes': 'MAX_INPUT_BYTES', 'job-bytes': 'MAX_JOB_INPUT_BYTES',
                      'depth': 'MAX_SCAN_DEPTH'}[limit]
    monkeypatch.setattr(input_selection, selected_limit, 1)
    if limit == 'depth':
        (folder / 'subfolder').mkdir()
    with pytest.raises(ValueError, match='budget'):
        select_inputs(folders=(folder,), recursive=True)


def test_folder_selection_cancellation_discards_the_unfinished_list(tmp_path):
    folder = folder_sources(tmp_path)
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 6
    with pytest.raises(InterruptedError, match='selection cancelled'):
        select_inputs(folders=(folder,), cancelled=cancelled)


def test_folder_does_not_descend_reparse_entry(tmp_path, monkeypatch):
    folder = folder_sources(tmp_path)
    excluded = folder / 'junction'; excluded.mkdir()
    (excluded / 'private.wav').write_bytes(b'never selected')
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path == excluded:
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
        return value
    monkeypatch.setattr(Path, 'lstat', lstat)
    selection = select_inputs(folders=(folder,), recursive=True)
    assert len(selection.files) == 2
    assert 'Link or reparse point; not followed.' in selection.describe()


def test_folder_does_not_follow_symbolic_or_hard_links(tmp_path):
    folder = folder_sources(tmp_path)
    outside = tmp_path / 'outside'; outside.mkdir()
    private = outside / 'private.wav'; private.write_bytes(b'not selected')
    os.link(private, folder / 'hard.wav')
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(outside), str(folder / 'linked-folder'))
    else:
        (folder / 'linked-folder').symlink_to(outside, target_is_directory=True)
    selected = select_inputs(folders=(folder,), recursive=True)
    assert len(selected.files) == 2 and selected.skipped_count == 3
    assert 'not followed' in selected.describe() and 'unlinked local file' in selected.describe()
    with pytest.raises(ValueError, match='without links'):
        select_inputs(folders=(folder / 'linked-folder',), recursive=True)


def test_folder_changed_during_preflight_is_not_an_accepted_partial_selection(tmp_path, monkeypatch):
    folder = folder_sources(tmp_path)
    original = input_selection.regular
    changed = False
    def regular(path, **kwargs):
        nonlocal changed
        value = original(path, **kwargs)
        if Path(path).name == 'A.WAV' and not changed:
            changed = True
            (folder / 'late.wav').write_bytes(b'late source')
        return value
    monkeypatch.setattr(input_selection, 'regular', regular)
    with pytest.raises(ValueError, match='folder changed'):
        select_inputs(folders=(folder,))


def test_cli_folder_review_discloses_skips_and_retains_only_selected_files(tmp_path):
    folder = folder_sources(tmp_path)
    root = tmp_path / 'library'
    result = cli(root, 'submit', '--folder', folder, '--language', 'en')
    row = payload(result)
    assert row['state'] == 'queued' and row['total'] == 2
    assert 'notes.txt' in result.stderr and 'Unsupported audio/video extension' in result.stderr
    saved = payload(cli(root, 'status', row['id']))
    assert [item['name'] for item in saved['inputs']] == ['A.WAV', 'b.wav']
    assert not (root / 'state/transcription/jobs' / row['id'] / 'job_config.json').exists()


def test_cli_empty_or_unsafe_folder_never_initializes_library(tmp_path):
    folder = tmp_path / 'empty'; folder.mkdir()
    (folder / 'notes.txt').write_text('Only unsupported input')
    root = tmp_path / 'unused'
    result = cli(root, 'submit', '--folder', folder)
    assert result.returncode == 1 and 'No supported' in result.stderr and 'notes.txt' in result.stderr
    assert not root.exists()
    result = cli(root, 'submit', '--folder', folder / 'notes.txt')
    assert result.returncode == 1 and not root.exists()


def test_folder_batch_cancel_then_resume_uses_same_retained_two_input_job(tmp_path, engine, monkeypatch):
    folder = folder_sources(tmp_path)
    root = tmp_path / 'library'
    spec = engine()
    payload(configure(root, spec))
    identity = payload(cli(root, 'submit', '--folder', folder))['id']
    service = transcription.TranscriptionService(root / 'state' / 'transcription')
    before = service.get(identity)
    expected = {row['name']: (folder / row['name']).read_bytes() for row in before['inputs']}
    for path in folder.iterdir():
        path.unlink()
    folder.rmdir()
    cancellation = threading.Event()
    original = service._artifact
    def verified(*args, **kwargs):
        result = original(*args, **kwargs)
        cancellation.set()
        return result
    monkeypatch.setattr(service, '_artifact', verified)
    cancelled = service.run(identity, cancelled=cancellation.is_set)
    assert cancelled['id'] == identity and cancelled['state'] == 'cancelled'
    job_root = service.jobs / identity
    assert read_json(job_root / 'job_result.json')['total'] == 2
    assert len(list((job_root / 'results').iterdir())) == 8
    for row in cancelled['inputs']:
        assert (job_root / row['path']).read_bytes() == expected[row['name']]
    resumed = payload(cli(root, 'run', identity, admitted=spec.worker_sha256))
    assert resumed['id'] == identity and resumed['state'] == 'succeeded'
    assert resumed['receipt']['done'] == 2 and len(resumed['receipt']['artifacts']) == 8
    assert resumed['inputs'] == before['inputs']
    assert read_json(job_root / 'job_config.json')['folders'] == []


def test_cli_waiting_capability_and_jobs_survive_reopening(tmp_path):
    root = tmp_path / 'library'
    assert payload(cli(root,'capability'))['state'] == 'waiting-input'
    assert payload(cli(root,'jobs')) == []
    assert (root / 'library.sqlite').is_file()
    assert payload(cli(root,'capability'))['state'] == 'waiting-input'


def test_cli_rejects_unknown_worker_before_creating_library(tmp_path, engine):
    root = tmp_path / 'library'
    result = configure(root,engine(),admitted=False)
    assert result.returncode == 1 and 'not been admitted' in result.stderr
    assert not root.exists()


@pytest.mark.parametrize('value', [[], {'unknown':'value'}, {'language':'uk','model':'tiny.en'}, {'parallel_files':2}])
def test_invalid_settings_json_has_no_application_side_effects(tmp_path, source, value):
    root = tmp_path / 'library'
    settings = tmp_path / 'settings.json';settings.write_text(json.dumps(value))
    result = cli(root,'submit',source,'--settings',settings)
    assert result.returncode == 1
    assert not root.exists()


def test_settings_file_and_flags_cannot_be_mixed(tmp_path, source):
    root = tmp_path / 'library'
    settings = tmp_path / 'settings.json';settings.write_text('{}')
    result = cli(root,'submit',source,'--settings',settings,'--language','en')
    assert result.returncode == 2 and not root.exists()


def test_submit_without_engine_is_queued_and_does_not_execute(tmp_path, source):
    root = tmp_path / 'library'
    original = source.read_bytes()
    row = payload(cli(root,'submit',source,'--title','Saved CLI job','--language','en','--format','txt'))
    assert row['state'] == 'queued' and row['artifacts'] == [] and row['total'] == 1
    assert 'Copying selected audio' in row_to_progress(root,source)
    stored = payload(cli(root,'status',row['id']))
    assert stored['settings']['output_txt'] and not stored['settings']['output_srt']
    assert stored['receipt'] is None and stored['state'] == 'queued'
    assert source.read_bytes() == original
    assert not (root / 'state/transcription/jobs' / row['id'] / 'job_config.json').exists()
    assert len(payload(cli(root,'jobs'))) == 2
    failure = cli(root,'run',row['id'])
    assert failure.returncode == 1 and 'Configure the reviewed local' in failure.stderr
    assert payload(cli(root,'status',row['id']))['state'] == 'queued'


def row_to_progress(root,source):
    # A second explicit submit is a separate queued job; stdout remains JSON.
    result = cli(root,'submit',source)
    assert payload(result)['state'] == 'queued'
    return result.stderr


def test_cli_real_worker_protocol_edit_export_and_persistence(tmp_path, source, engine):
    root = tmp_path / 'library';spec=engine()
    assert payload(configure(root,spec))['engine']['worker_sha256'] == spec.worker_sha256
    row=payload(cli(root,'submit',source,'--language','en'))
    identity=row['id']
    result=cli(root,'run',identity,admitted=spec.worker_sha256)
    job=payload(result)
    assert job['state']=='succeeded' and len(job['receipt']['artifacts'])==4
    assert 'Transcription job: '+identity in result.stderr
    assert 'Verified every requested' in result.stderr
    item=next(a for a in job['receipt']['artifacts'] if a['format']=='txt')
    text=tmp_path/'edit.txt';text.write_text('Нова похідна CLI версія.',encoding='utf-8')
    revision=payload(cli(root,'edit',identity,'--artifact',item['path'],'--text-file',text))
    destination=tmp_path/'new.txt'
    exported=payload(cli(root,'export',identity,'--artifact',item['path'],'--revision',revision['id'],'--output',destination))
    assert exported['state']=='exported' and destination.read_text(encoding='utf-8')=='Нова похідна CLI версія.'
    assert cli(root,'export',identity,'--artifact',item['path'],'--output',destination).returncode==1
    assert destination.read_text(encoding='utf-8')=='Нова похідна CLI версія.'
    reopened=payload(cli(root,'status',identity))
    assert reopened['revisions'][0]['id']==revision['id'] and reopened['state']=='succeeded'
    assert (root/'state/transcription/jobs'/identity/item['path']).read_text().startswith('Synthetic transcript')


def test_cli_invalid_worker_terminal_claim_returns_nonzero_and_retains_job(tmp_path, source, engine):
    root=tmp_path/'library';spec=engine('wrong-exit')
    payload(configure(root,spec))
    identity=payload(cli(root,'submit',source))['id']
    result=cli(root,'run',identity,admitted=spec.worker_sha256)
    assert result.returncode==1 and 'success does not match' in result.stderr
    job=payload(cli(root,'status',identity))
    assert job['state']=='failed' and job['receipt'] is None


def test_cli_pause_continue_and_ctrl_c_retain_resumable_job(tmp_path, source, engine):
    root=tmp_path/'library';spec=engine('pause')
    payload(configure(root,spec))
    identity=payload(cli(root,'submit',source))['id']
    trigger=tmp_path/'send-python-sigint'
    cmd,environment=command(['transcription','--data-dir',str(root),'run',identity],
                             admitted=spec.worker_sha256,interrupt_trigger=trigger)
    process=subprocess.Popen(cmd,env=environment,cwd=root.parent,text=True,encoding='utf-8',stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    directory=root/'state/transcription/jobs'/identity
    try:
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if (directory/'test_started.json').exists():break
            if process.poll() is not None:break
            time.sleep(.02)
        assert process.poll() is None and (directory/'test_started.json').exists()
        assert payload(cli(root,'pause',identity))['state']=='pause-requested'
        assert (directory/'pause.flag').exists()
        # Each synthetic acknowledgement is published once; the replaceable
        # live status is never opened by either the CLI or this observer.
        deadline=time.monotonic()+4
        while not (directory/'test_pause_seen.json').exists() and time.monotonic()<deadline:
            assert process.poll() is None
            time.sleep(.02)
        assert (directory/'test_pause_seen.json').exists()
        assert payload(cli(root,'continue',identity))['state']=='continue-requested'
        assert not (directory/'pause.flag').exists()
        deadline=time.monotonic()+4
        while not (directory/'test_continue_seen.json').exists() and time.monotonic()<deadline:
            assert process.poll() is None
            time.sleep(.02)
        assert (directory/'test_continue_seen.json').exists()
        trigger.write_text('send SIGINT through Python signal machinery')
        output,error=process.communicate(timeout=12)
        assert process.returncode==130, output+'\n'+error
        assert json.loads(output)['state']=='cancelled'
        assert 'Explicitly run the saved job ID' in error
        started=read_json(directory/'test_started.json')
        paused=read_json(directory/'test_pause_seen.json')
        continued=read_json(directory/'test_continue_seen.json')
        terminal=read_json(directory/'job_result.json')
        assert started==paused==continued=={'job_id':identity,'run_id':terminal['run_id']}
        job=payload(cli(root,'status',identity))
        assert job['state']=='cancelled' and (directory/job['inputs'][0]['path']).read_bytes()==source.read_bytes()
    except BaseException as error:
        # A running CLI and a status filename alone cannot prove its worker is
        # still alive. Keep the actual run outcome and durable error in the
        # failure report instead of losing them during cleanup.
        trigger.write_text('stop for failed assertion', encoding='utf-8')
        try:
            output, diagnostic = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill(); output, diagnostic = process.communicate(timeout=5)
        error.add_note('Owned ASR run exit=' + str(process.returncode) + '\nstdout=' + output[-8000:]
                       + '\nstderr=' + diagnostic[-8000:])
        try:
            saved = read_json(directory / 'job.json')
            error.add_note('Durable ASR state=' + str(saved['state']) + '; error=' + str(saved.get('error')))
        except (OSError, ValueError) as state_error:
            error.add_note('Could not read ASR state: ' + str(state_error))
        raise
    finally:
        trigger.write_text('stop')
        if process.poll() is None:
            try:process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill();process.communicate(timeout=5)


def test_main_dispatch_preserves_existing_workers_studio_and_assistant(monkeypatch):
    from types import SimpleNamespace
    from carr_library.__main__ import main
    called=[]
    monkeypatch.setitem(sys.modules,'carr_library.academic_integration.doi',SimpleNamespace(_worker_main=lambda a:called.append(('doi',a)) or 11))
    monkeypatch.setitem(sys.modules,'carr_library.speech.sapi_native_worker',SimpleNamespace(main=lambda a:called.append(('sapi',a)) or 12))
    monkeypatch.setitem(sys.modules,'carr_library.studio.cli',SimpleNamespace(main=lambda a:called.append(('studio',a)) or 13,
        assistant_main=lambda a:called.append(('assistant',a)) or 14))
    assert main(['--carr-doi-metadata-worker','--request','fixture'])==11
    assert main(['--carr-sapi-native-worker','request','result'])==12
    assert main(['studio','jobs'])==13
    assert main(['assistant','--help'])==14
    assert [entry[0] for entry in called]==['doi','sapi','studio','assistant']


@pytest.mark.parametrize('kind', ['missing','duplicate','unsupported','empty-title'])
def test_invalid_submit_path_arguments_do_not_initialize_library(tmp_path, source, kind):
    root=tmp_path/'unused'
    args=['submit',str(source)]
    if kind=='missing':args=['submit',str(tmp_path/'missing.wav')]
    if kind=='duplicate':args.append(str(source))
    if kind=='unsupported':
        bad=tmp_path/'note.txt';bad.write_text('not audio');args=['submit',str(bad)]
    if kind=='empty-title':args.extend(['--title',''])
    result=cli(root,*args)
    assert result.returncode==1 and not root.exists()


def test_valid_failed_terminal_result_is_json_with_exit_one(tmp_path, source, engine):
    from dataclasses import replace
    from hashlib import sha256
    spec=engine()
    path=Path(spec.worker_path)
    script=path.read_text(encoding='utf-8')
    marker="if MODE=='wrong-job': value['job_id']='0'*32"
    script=script.replace(marker, """value['state']='failed';value['done']=0;value['failed']=len(records)
for record in records: record['state']='failed';record['error']='Synthetic engine failure';record['artifacts']=[]
value['artifacts']=[]
write('job_result.json',value);status('terminal',0,'failed');sys.exit(1)
"""+marker)
    path.write_text(script,encoding='utf-8')
    spec=replace(spec,worker_sha256=sha256(path.read_bytes()).hexdigest())
    root=tmp_path/'library';payload(configure(root,spec))
    identity=payload(cli(root,'submit',source))['id']
    result=cli(root,'run',identity,admitted=spec.worker_sha256)
    job=payload(result,expected=1)
    assert job['state']=='failed' and job['receipt']['failed']==1
    assert payload(cli(root,'status',identity))['state']=='failed'
