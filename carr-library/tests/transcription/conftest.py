"""Independent controlled protocol backend; no private ASR code or real model.

The fixture executes a real isolated child interpreter and emits deterministic
Unicode text. Synthetic package metadata and model bytes are deliberately not
recognition dependencies or an inference/accuracy acceptance claim.
"""
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys
import venv

import pytest

from carr_library.application import LibraryApp
from carr_library.transcription import TranscriptionService
from carr_library.transcription.runtime import PACKAGES


BACKEND = r'''
import argparse, hashlib, importlib.metadata, json, os, pathlib, sys, time
p=argparse.ArgumentParser();p.add_argument('--job-dir',required=True)
job=pathlib.Path(p.parse_args().job_dir)
config=json.loads((job/'job_config.json').read_text('utf-8'))
settings=json.loads((job/'settings_live.json').read_text('utf-8'))
mode=json.loads(pathlib.Path(__file__).with_name('fixture.json').read_text())['mode']
assert os.environ['HF_HUB_OFFLINE']=='1' and os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN']=='1'
assert not any(k in os.environ for k in ('HF_TOKEN','PRIVATE_API_KEY','HTTPS_PROXY','PYTHONPATH'))
def raw(v): return (json.dumps(v,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
def digest(p):
 b=p.read_bytes();return {'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()}
def write(p,b):
 p.parent.mkdir(parents=True,exist_ok=True)
 with p.open('wb') as f:f.write(b);f.flush();os.fsync(f.fileno())
sources=sorted(pathlib.Path(config['folders'][0]).iterdir())
def status(state,done=0):
 value={'state':state,'done':done,'failed':0,'total':len(sources),
        'results_dir':'/status/path/has/no/authority','current':'ignored untrusted source path'}
 write(job/'job_status.json',raw(value))
status('обробка')
write(job/'logs'/'fixture.started',b'controlled backend only')
if mode=='network':
 import socket
 try:socket.socket().connect(('127.0.0.1',9))
 except PermissionError:write(job/'logs'/'network.denied',b'yes')
 else:raise AssertionError('Network audit did not refuse connect')
if mode=='stuck':time.sleep(60)
until=time.monotonic()+(.4 if mode=='slow' else 0)
while (job/'pause.flag').exists() or time.monotonic()<until:
 if (job/'stop.flag').exists():
  write(job/'logs'/'controlled.partial.json',raw({'complete':False}));status('зупинено');sys.exit(2)
 if (job/'pause.flag').exists():status('paused')
 time.sleep(.01)
if mode=='grow':write(job/'oversized.tmp',b'x'*(2*1024*1024))
if mode=='log':sys.stdout.write('x'*(2*1024*1024));sys.stdout.flush()
if mode=='input':write(sources[0],b'changed snapshot')
model=pathlib.Path(config['models_dir'])/settings['model']
model_identity={'selection':settings['model'],'verified':True,
 'files':[dict(path=p.relative_to(model).as_posix(),**digest(p)) for p in sorted(model.rglob('*')) if p.is_file()]}
runtime={'python':sys.version,'platform':sys.platform,
 'packages':{n:importlib.metadata.version(n) for n in
 ('faster-whisper','ctranslate2','av','tokenizers','onnxruntime','numpy')},
 'worker_sha256':digest(pathlib.Path(__file__))['sha256']}
excluded={'output_mode','include_headers','output_txt','output_srt','output_vtt','output_json'}
for count, source in enumerate(sources,1):
 text='Controlled English translation.' if settings['task']=='translate' else 'Контрольний текст — synthetic protocol, not ASR.'
 segment={'id':0,'seek':0,'start':.25,'end':1.75,'text':text,'words':[]}
 owner={'schema':1,'source':str(source),'locator':{'group':'inputs','relative':source.name,'root':str(source.parent)},
 'audio':digest(source),'model':model_identity,'runtime':runtime,
 'settings':{k:v for k,v in settings.items() if k not in excluded}}
 meta={'source':str(source),'group':'inputs','relative':source.name,'created':'2026-01-01T00:00:00',
 'selected_language':settings['language'],'detected_language':'uk','duration':2.,'settings':dict(settings),
 'segments':[segment],'text':text,'v43_attempt':'original','v43_attempt_no':1,
 'requested_settings':dict(settings),'reuse_identity':owner}
 if mode=='owner':meta['reuse_identity']=dict(owner,source='/unapproved/input.wav')
 if mode=='settings':meta['settings']['task']='unapproved'
 if mode=='language':meta['detected_language']='zz'
 if mode=='nan':segment['end']=float('nan')
 if mode=='duration':segment['end']=30.
 payload={'txt':(text+'\n').encode(),'json':raw(meta),
 'srt':('1\n00:00:00,250 --> 00:00:01,750\n'+text+'\n').encode(),
 'vtt':('WEBVTT\n\n00:00:00.250 --> 00:00:01.750\n'+text+'\n').encode()}
 if mode=='empty':
  meta['text']='';meta['segments']=[]
  payload={'txt':b'','srt':b'','vtt':b'WEBVTT\n\n','json':raw(meta)}
 if mode=='timestamp':payload['srt']=('1\n00:00:00,250 --> 00:00:60,000\n'+text+'\n').encode()
 if mode=='text':payload['txt']=b'different unverified text\n'
 for fmt in ('txt','srt','vtt','json'):
  if not settings['output_'+fmt]:continue
  path=job/'results'/'inputs'/(source.stem+'.'+fmt)
  b=payload[fmt]
  proof={'schema':1,'owners':[owner],
   'export_settings':{'output_mode':'separate','include_headers':False} if fmt=='txt' else {},
   'sha256':hashlib.sha256(b).hexdigest(),'bytes':len(b)}
  if mode=='proof':proof['sha256']='0'*64
  write(path,b);write(path.with_name(path.name+'.v43-output.json'),raw(proof))
 status('обробка',count)
if mode=='missing':(job/'results'/'inputs'/(sources[0].stem+'.txt')).unlink()
if mode=='extra':write(job/'results'/'outside.txt',b'unexpected')
status('завершено',0 if mode=='count' else len(sources))
'''


@pytest.fixture(scope='session')
def protocol_runtime_template(tmp_path_factory):
    root = tmp_path_factory.mktemp('controlled-transcription-runtime')
    venv.EnvBuilder(with_pip=False, symlinks=False).create(root / 'python')
    # CPython creates an unnecessary lib64 alias even with symlinks=False.
    # The reviewed fixture installation contains ordinary files only.
    alias = root / 'python' / 'lib64'
    if alias.is_symlink():
        alias.unlink()
    python = root / 'python' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    if sys.platform != 'win32':
        for name in ('python3', 'python' + str(sys.version_info.major) + '.' + str(sys.version_info.minor)):
            duplicate = python.parent / name
            if duplicate != python:
                duplicate.unlink(missing_ok=True)
    probe = "import sys,sysconfig,json;print(json.dumps([sys.version,sys.platform,sysconfig.get_path('purelib')]))"
    version, platform, site = json.loads(subprocess.check_output([python, '-I', '-B', '-c', probe], text=True))
    for package in PACKAGES:
        directory = Path(site) / (package.replace('-', '_') + '-0.0.0.dist-info')
        directory.mkdir(parents=True)
        (directory / 'METADATA').write_text(f'Metadata-Version: 2.1\nName: {package}\nVersion: 0.0.0\n', encoding='utf-8')
    (root / 'worker.py').write_text(BACKEND, encoding='utf-8')
    model = root / 'models' / 'controlled-fixture'
    model.mkdir(parents=True)
    for name in ('model.bin', 'config.json', 'tokenizer.json'):
        (model / name).write_bytes(b'synthetic protocol bytes; not a model')
    return root, python.relative_to(root).as_posix(), {'python': version, 'platform': platform,
        'packages': {name: '0.0.0' for name in PACKAGES}}


@pytest.fixture
def setup(tmp_path, protocol_runtime_template):
    template, python, runtime = protocol_runtime_template
    installations = []
    def create(mode='valid'):
        root = tmp_path / ('runtime-' + mode)
        installations.append(root)
        shutil.copytree(template, root)
        (root / 'fixture.json').write_text(json.dumps({'mode': mode}), encoding='utf-8')
        settings = {'protocol': 'whisper-transcribe-job-dir-v43', 'python': python, 'worker': 'worker.py',
                    'models': 'models', 'model': 'controlled-fixture', 'runtime': runtime,
                    'languages': ['uk', 'en'], 'inference_profiles': [{'device': 'cpu', 'compute_type': 'int8'}]}
        manifest = {'schema_version': 1, 'engine': 'whisper-transcribe',
            'model_id': 'controlled-protocol-fixture-not-a-real-model', 'revision': '1' * 40,
            'license': 'synthetic-only', 'license_accepted': True, 'trusted_weights': True,
            'files': {p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest()
                      for p in root.rglob('*') if p.is_file()}, 'settings': settings}
        path = root / 'profile.json'
        path.write_text(json.dumps(manifest), encoding='utf-8')
        source = tmp_path / (mode + '.recording.wav')
        source.write_bytes(b'RIFF synthetic explicit source; no real person\x00\x01')
        app = LibraryApp(tmp_path / ('library-' + mode))
        workspace = app.create_workspace('Controlled transcription scope')
        return TranscriptionService(app), workspace, path, source
    yield create
    # Keep semantic artifacts for pytest diagnostics, not per-case interpreter
    # copies. This also bounds disk use when many suites run on the same worker.
    for root in installations:
        shutil.rmtree(root, ignore_errors=True)

