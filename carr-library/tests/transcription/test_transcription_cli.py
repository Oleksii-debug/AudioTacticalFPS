"""Actual CLI subprocesses share private jobs; synthetic admission is test-only."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

import carr_library.transcription as transcription
from carr_library.durable_state import read_json

_fixture_spec = importlib.util.spec_from_file_location('transcription_cli_fixtures', Path(__file__).with_name('test_transcription.py'))
_fixtures = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixtures)
engine = _fixtures.engine
source = _fixtures.source


def command(arguments, *, admitted=None, interrupt_trigger=None):
    environment = dict(os.environ)
    package_root = str(Path(transcription.__file__).resolve().parents[1])
    environment['PYTHONPATH'] = package_root + os.pathsep + environment.get('PYTHONPATH', '')
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
    return subprocess.run(cmd, env=environment, text=True, encoding='utf-8', capture_output=True, timeout=timeout)


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
                 ('edit','0'*32,'--artifact','../outside.txt','--text-file','missing')]:
        result = cli(root,*args)
        assert result.returncode == 2, result.stderr
        assert not root.exists(), args


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
    root=tmp_path/'library';spec=engine('cooperative')
    payload(configure(root,spec))
    identity=payload(cli(root,'submit',source))['id']
    trigger=tmp_path/'send-python-sigint'
    cmd,environment=command(['transcription','--data-dir',str(root),'run',identity],
                             admitted=spec.worker_sha256,interrupt_trigger=trigger)
    process=subprocess.Popen(cmd,env=environment,text=True,encoding='utf-8',stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    directory=root/'state/transcription/jobs'/identity
    try:
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if (directory/'job_status.json').exists():break
            if process.poll() is not None:break
            time.sleep(.02)
        assert process.poll() is None and (directory/'job_status.json').exists()
        assert payload(cli(root,'pause',identity))['state']=='pause-requested'
        assert (directory/'pause.flag').exists()
        assert payload(cli(root,'continue',identity))['state']=='continue-requested'
        assert not (directory/'pause.flag').exists()
        trigger.write_text('send SIGINT through Python signal machinery')
        output,error=process.communicate(timeout=12)
        assert process.returncode==130, output+'\n'+error
        assert json.loads(output)['state']=='cancelled'
        assert 'Explicitly run the saved job ID' in error
        job=payload(cli(root,'status',identity))
        assert job['state']=='cancelled' and (directory/job['inputs'][0]['path']).read_bytes()==source.read_bytes()
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
    script=path.read_text()
    marker="if MODE=='wrong-job': value['job_id']='0'*32"
    script=script.replace(marker, """value['state']='failed';value['done']=0;value['failed']=len(records)
for record in records: record['state']='failed';record['error']='Synthetic engine failure';record['artifacts']=[]
value['artifacts']=[]
write('job_result.json',value);status('terminal',0,'failed');sys.exit(1)
"""+marker)
    path.write_text(script)
    spec=replace(spec,worker_sha256=sha256(path.read_bytes()).hexdigest())
    root=tmp_path/'library';payload(configure(root,spec))
    identity=payload(cli(root,'submit',source))['id']
    result=cli(root,'run',identity,admitted=spec.worker_sha256)
    job=payload(result,expected=1)
    assert job['state']=='failed' and job['receipt']['failed']==1
    assert payload(cli(root,'status',identity))['state']=='failed'
