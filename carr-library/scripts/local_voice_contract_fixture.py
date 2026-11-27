"""Public deterministic JSON-voice fixture, never a speech-quality substitute.

A fresh copied, no-pip venv supplies the explicitly pinned interpreter. Its
pyvenv.cfg refers to the selected test host's trusted standard library; this is
not a claim that the fixture is an independent redistributed Python runtime.
Every selected-pack file is hashed. The fixture emits deterministic PCM only.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import venv

SCHEMA = 'carr.local-voice.v1'
VOICE = {'id': 'synthetic-uk-token', 'name': 'Synthetic Київ voice',
         'language': 'uk-UA', 'license': 'Public synthetic acceptance fixture',
         'capabilities': ['rate', 'volume', 'pitch', 'punctuation', 'phonemes']}

CHILD = r'''
import hashlib, json, os, pathlib, struct, sys, time, wave
MODE = __MODE__
ROWS = __ROWS__
SCHEMA = 'carr.local-voice.v1'
def offline(event, args):
    if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system', 'os.posix_spawn'):
        raise OSError('The public synthetic helper has no network or descendant authority.')
sys.addaudithook(offline)
raw = sys.stdin.buffer.read(128001)
if len(raw) > 128000:
    raise SystemExit(4)
request = json.loads(raw.decode('utf-8'))
if request.get('schema') != SCHEMA or not sys.flags.isolated or not sys.dont_write_bytecode:
    raise SystemExit(5)
if any(key in os.environ for key in ('CARR_FIXTURE_API_KEY', 'CARR_FIXTURE_PASSWORD',
                                    'PYTHONPATH', 'PYTHONHOME', 'LD_PRELOAD')):
    raise SystemExit(6)
if request.get('operation') == 'voices':
    if set(request) != {'schema', 'operation'}:
        raise SystemExit(7)
    if MODE == 'duplicate-json':
        sys.stdout.write('{"schema":"carr.local-voice.v1","voices":[],"voices":[]}')
    elif MODE == 'nonfinite-json':
        sys.stdout.write('{"schema":"carr.local-voice.v1","voices":NaN}')
    elif MODE == 'invalid-utf8':
        sys.stdout.buffer.write(b'\xff')
    elif MODE == 'log-overflow':
        sys.stdout.write('x' * (1024 * 1024 + 1))
    elif MODE == 'wrong-schema':
        print(json.dumps({'schema': 'unexpected', 'voices': ROWS}))
    elif MODE == 'reply-authority':
        print(json.dumps({'schema': SCHEMA, 'voices': ROWS, 'executable': 'do-not-run'}))
    else:
        print(json.dumps({'schema': SCHEMA, 'voices': ROWS}, ensure_ascii=False))
elif request.get('operation') == 'render':
    if set(request) != {'schema', 'operation', 'voice_id', 'text', 'output', 'settings'}:
        raise SystemExit(8)
    settings = request['settings']
    if set(settings) != {'rate', 'volume', 'pitch', 'punctuation', 'timeout_seconds', 'max_output_bytes'}:
        raise SystemExit(9)
    output = pathlib.Path(request['output'])
    if output.name != 'speech.wav' or output.parent.resolve() != pathlib.Path.cwd().resolve():
        raise SystemExit(10)
    if any(pathlib.Path(os.environ[name]).resolve() != output.parent.resolve()
           for name in ('TEMP', 'TMP', 'TMPDIR')):
        raise SystemExit(11)
    if MODE == 'render-exit':
        raise SystemExit(13)
    value = {key: request[key] for key in ('voice_id', 'text', 'settings')}
    digest = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).digest()
    with wave.open(str(output), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(1 if MODE == 'wrong-pcm' else 2)
        audio.setframerate(16000)
        audio.writeframes(digest * 32)
    if MODE == 'truncated-pcm':
        with output.open('r+b') as audio:
            audio.truncate(50)
    if MODE == 'hang-render':
        while True:
            time.sleep(.02)
    if MODE == 'render-authority':
        print(json.dumps({'schema': SCHEMA, 'status': 'rendered', 'output': str(output)}))
    elif MODE == 'wrong-status':
        print(json.dumps({'schema': SCHEMA, 'status': 'queued'}))
    else:
        print(json.dumps({'schema': SCHEMA, 'status': 'rendered'}))
else:
    raise SystemExit(12)
'''


def sha(path):
    digest = sha256()
    with Path(path).open('rb') as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def interpreter_template(directory):
    root = Path(directory)
    if root.exists():
        raise FileExistsError(root)
    venv.EnvBuilder(with_pip=False, symlinks=False).create(root)
    # CPython's POSIX venv may add only this owned convenience directory link.
    alias = root / 'lib64'
    if alias.is_symlink() and alias.resolve(strict=True) == (root / 'lib').resolve(strict=True):
        alias.unlink()
    for path in root.rglob('*'):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('The synthetic interpreter must contain no linked files.')
    return root


def installation(directory, *, template=None, mode='good', voices=None, settings=None):
    root = Path(directory)
    root.mkdir()
    if template is None:
        interpreter_template(root / 'python')
    else:
        shutil.copytree(template, root / 'python', symlinks=False)
    rows = [VOICE] if voices is None else voices
    script = CHILD.replace('__MODE__', repr(mode)).replace('__ROWS__', repr(rows))
    (root / 'helper.py').write_text(script, encoding='utf-8')
    executable = 'python/Scripts/python.exe' if os.name == 'nt' else 'python/bin/python'
    options = {'protocol': SCHEMA, 'launch': 'python', 'executable': executable,
               'script': 'helper.py', 'engine_name': 'Public deterministic helper',
               'timeout_seconds': 15}
    if settings:
        options.update(settings)
    value = {'schema_version': 1, 'engine': 'local-voice-helper', 'model_id': 'public-helper',
             'revision': sha256(script.encode('utf-8')).hexdigest(), 'license': 'Public synthetic acceptance fixture',
             'license_accepted': True, 'trusted_weights': True,
             'files': {path.relative_to(root).as_posix(): sha(path)
                       for path in sorted(root.rglob('*')) if path.is_file()},
             'settings': options}
    manifest = root / 'manifest.json'
    manifest.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest


def expected_pcm(text, *, rate=0, volume=1, pitch=0, punctuation=False, timeout=15,
                 maximum=256 * 1024 * 1024):
    value = {'voice_id': VOICE['id'], 'text': text,
             'settings': {'rate': rate, 'volume': round(volume * 100),
                          'pitch': None if pitch == 0 else round(50 + pitch * 3),
                          'punctuation': punctuation, 'timeout_seconds': float(timeout),
                          'max_output_bytes': maximum}}
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(',', ':')).encode('utf-8')).digest() * 32


def identity(manifest):
    path = Path(manifest)
    value = json.loads(path.read_text(encoding='utf-8'))
    executable = path.parent / value['settings']['executable']
    return {'manifest_sha256': sha(path), 'fixture_script_sha256': sha(path.parent / 'helper.py'),
            'selected_python_sha256': sha(executable),
            'host_python_sha256': sha(Path(sys._base_executable)),
            'host_python_version': sys.version, 'files': len(value['files']),
            'bytes': sum((path.parent / name).stat().st_size for name in value['files']),
            'launch': value['settings']['launch'], 'protocol': SCHEMA}
