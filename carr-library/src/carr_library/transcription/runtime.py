"""Private installation identity and fixed offline job-directory launch.

The reviewed interpreter, native libraries and OS remain trusted software.
Python audit hooks and offline flags are not a sandbox for hostile native code.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re

from ..audio.engines import _run
from ..audio.model_manifest import LocalModelManifest, ModelManifestError, local_path
from .files import canonical, parse_json, regular
from .models import PROTOCOL, TranscriptionOptions, checksum, exact_keys


PACKAGES = ('faster-whisper', 'ctranslate2', 'av', 'tokenizers', 'onnxruntime', 'numpy')
_PROBE = '''import importlib.metadata,json,sys
p={}
for n in ('faster-whisper','ctranslate2','av','tokenizers','onnxruntime','numpy'):
 try: p[n]=importlib.metadata.version(n)
 except importlib.metadata.PackageNotFoundError: p[n]=None
print(json.dumps({'python':sys.version,'platform':sys.platform,'packages':p},sort_keys=True))
'''
_BOOTSTRAP = '''import os,pathlib,runpy,stat,sys
worker=pathlib.Path(sys.argv[1])
lock=pathlib.Path(sys.argv[-1])/'worker-lock'/'.render.lock'
descriptor=os.open(lock,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
info=os.fstat(descriptor)
if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or getattr(info,'st_file_attributes',0)&0x400:
 raise ValueError('Invalid transcription worker lock')
if not info.st_size: os.write(descriptor,b'0')
os.lseek(descriptor,0,os.SEEK_SET)
if os.name=='nt':
 import msvcrt
 msvcrt.locking(descriptor,msvcrt.LK_NBLCK,1)
else:
 import fcntl
 fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
def offline(event,args):
 if event in {'socket.connect','socket.connect_ex','socket.bind','socket.getaddrinfo','socket.sendto','urllib.Request','subprocess.Popen','os.system','os.posix_spawn'}:
  raise PermissionError('Local transcription denies network and child-process execution')
sys.addaudithook(offline)
sys.path.insert(0,str(worker.parent))
sys.argv=sys.argv[1:]
runpy.run_path(str(worker),run_name='__main__')
'''


def _relative(value):
    if (not isinstance(value, str) or not value or len(value) > 1024 or '\\' in value
            or ':' in value or PurePosixPath(value).is_absolute()
            or any(p in ('', '.', '..') for p in value.split('/'))
            or any(ord(c) < 32 for c in value)):
        raise ModelManifestError('The runtime profile requires simple relative installation paths.')
    return value


def environment(job_directory):
    # Do not forward arbitrary provider secrets, proxy settings or Python hooks.
    allowed = {'SYSTEMROOT', 'WINDIR', 'LANG', 'LC_ALL', 'LC_CTYPE'}
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    root = str(job_directory)
    result.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_DATASETS_OFFLINE='1',
        HF_HUB_DISABLE_TELEMETRY='1', HF_HUB_DISABLE_IMPLICIT_TOKEN='1',
        DO_NOT_TRACK='1', TOKENIZERS_PARALLELISM='false', ORT_DISABLE_TELEMETRY='1',
        PYTHONDONTWRITEBYTECODE='1', HF_HOME=str(Path(root) / 'cache'),
        XDG_CACHE_HOME=str(Path(root) / 'cache'), TEMP=str(Path(root) / 'temp'),
        TMP=str(Path(root) / 'temp'))
    return result


@dataclass(frozen=True)
class TranscriptionRuntime:
    manifest_path: Path
    manifest_sha256: str
    python: Path
    worker: Path
    worker_sha256: str
    models: Path
    model: str
    model_id: str
    revision: str
    languages: tuple[str, ...]
    profiles: tuple[tuple[str, str], ...]
    runtime_json: bytes
    model_json: bytes

    @classmethod
    def load(cls, path, *, expected_sha256=None):
        manifest = LocalModelManifest.load(path, engine='whisper-transcribe', expected_sha256=expected_sha256)
        value = manifest.settings
        exact_keys(value, ('protocol', 'python', 'worker', 'models', 'model', 'runtime',
                           'languages', 'inference_profiles'), 'transcription profile')
        if value['protocol'] != PROTOCOL or not manifest.trusted_weights:
            raise ModelManifestError('Review the exact supplied worker, runtime and model before execution.')
        python = manifest.asset(_relative(value['python']))
        worker = manifest.asset(_relative(value['worker']))
        model = _relative(value['model'])
        if '/' in model:
            raise ModelManifestError('Choose one explicit installed model directory name.')
        models = local_path(manifest.root / _relative(value['models']))
        if not models.is_dir():
            raise ModelManifestError('The local models directory is missing.')
        prefix = value['models'] + '/' + model + '/'
        files = dict(manifest.files)
        for name in ('model.bin', 'config.json', 'tokenizer.json'):
            manifest.asset(prefix + name)
        entries = []
        for name, digest in manifest.files:
            if name.startswith(prefix):
                path = manifest.asset(name)
                entries.append({'path': name[len(prefix):], 'sha256': digest, 'bytes': regular(path).st_size})
        entries.sort(key=lambda row: Path(row['path']))
        actual = exact_keys(value['runtime'], ('python', 'platform', 'packages'), 'runtime identity')
        exact_keys(actual['packages'], PACKAGES, 'runtime package')
        if any(not isinstance(v, str) or not v or len(v) > 1024
               for v in (actual['python'], actual['platform'], *actual['packages'].values())):
            raise ModelManifestError('Pin exact interpreter and dependency versions; missing versions are not capabilities.')
        languages = value['languages']
        if (type(languages) is not list or not languages or len(languages) > 256
                or len(set(languages)) != len(languages)
                or any(not isinstance(v, str) or re.fullmatch('[a-z]{2,3}', v) is None for v in languages)):
            raise ModelManifestError('Declare only reviewed candidate input language codes.')
        profiles = value['inference_profiles']
        if type(profiles) is not list or not 1 <= len(profiles) <= 10:
            raise ModelManifestError('Declare bounded explicit device and compute profiles.')
        pairs = []
        for profile in profiles:
            exact_keys(profile, ('device', 'compute_type'), 'inference profile')
            TranscriptionOptions(device=profile['device'], compute_type=profile['compute_type'])
            pairs.append((profile['device'], profile['compute_type']))
        if len(set(pairs)) != len(pairs):
            raise ModelManifestError('Duplicate inference profile.')
        return cls(manifest.path, manifest.manifest_sha256, python, worker, files[value['worker']], models, model,
            manifest.model_id, manifest.revision, tuple(languages), tuple(pairs), canonical(actual),
            canonical({'selection': model, 'verified': True, 'files': entries}))

    def validate_options(self, options):
        if type(options) is not TranscriptionOptions:
            raise TypeError('Use typed transcription options.')
        if options.language != 'auto' and options.language not in self.languages:
            raise ModelManifestError('The selected profile does not advertise this input language.')
        if (options.device, options.compute_type) not in self.profiles:
            raise ModelManifestError('This device and compute combination is not in the reviewed profile.')

    def verify(self, *, check=lambda: None, timeout=30, probe_directory=None):
        check()
        manifest = LocalModelManifest.load(self.manifest_path, engine='whisper-transcribe',
                                           expected_sha256=self.manifest_sha256)
        def cancelled():
            check()
            return False
        manifest.verify(cancelled=cancelled, require_complete=True)
        # Reload immutable settings so a caller cannot substitute mutable dictionaries.
        if TranscriptionRuntime.load(self.manifest_path, expected_sha256=self.manifest_sha256) != self:
            raise ModelManifestError('The reviewed transcription installation changed.')
        raw = _run([self.python, '-I', '-B', '-c', _PROBE], timeout=timeout,
            cancelled=cancelled, env=environment(probe_directory or self.manifest_path.parent),
            cwd=self.worker.parent)
        if parse_json(raw) != parse_json(self.runtime_json):
            raise ModelManifestError('Installed interpreter or dependency versions differ from the private profile.')
        check()
        return self

    def command(self, job_directory):
        return [self.python, '-I', '-B', '-u', '-c', _BOOTSTRAP, self.worker,
                '--job-dir', Path(job_directory)]

    def worker_identity(self):
        return {**parse_json(self.runtime_json), 'worker_sha256': self.worker_sha256}
