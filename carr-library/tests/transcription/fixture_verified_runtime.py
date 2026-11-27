"""Build only synthetic distribution/model metadata for the versioned peer."""
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import shutil

_spec = importlib.util.spec_from_file_location('verified_fixture_base', Path(__file__).with_name('fixture_runtime.py'))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)


def add_distribution(site, name, *, requires=()):
    module = 'synthetic_' + name.replace('-', '_') + '.py'
    payload = site / module
    payload.write_text('# Synthetic identity only; this is not an installed engine.\n', encoding='utf-8')
    info = site / (name.replace('-', '_') + '-1.0.0.dist-info')
    info.mkdir()
    lines = ['Metadata-Version: 2.1', 'Name: ' + name, 'Version: 1.0.0']
    lines.extend('Requires-Dist: ' + requirement for requirement in requires)
    (info / 'METADATA').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    (info / 'RECORD').write_text('\n'.join([
        module + ',,', info.name + '/METADATA,,', info.name + '/RECORD,,']) + '\n', encoding='utf-8')
    return payload


def descriptor(runtime):
    def record(path):
        raw = path.read_bytes()
        return {'name': path.name, 'sha256': sha256(raw).hexdigest(), 'size_bytes': len(raw)}
    value = {'kind': 'whisper-transcribe-protocol', 'version': 1,
             'protocol': 'verified-file-job/1',
             'worker': record(runtime.worker),
             'helper': record(runtime.worker.with_name('recovery_support.py'))}
    path = runtime.worker.with_suffix('.protocol.json')
    path.write_text(json.dumps(value, sort_keys=True) + '\n', encoding='utf-8')
    return path


def make_runtime(root, *, options=None):
    runtime = _base.make_runtime(root, options=options)
    for info in runtime.site.glob('*.dist-info'):
        path = info / 'METADATA'
        text = path.read_text(encoding='utf-8').replace('Version: 0.0.synthetic', 'Version: 1.0.0')
        if 'Name: faster-whisper\n' in text:
            text += 'Requires-Dist: synthetic-helper>=1.0.0\n'
        path.write_text(text, encoding='utf-8')
    for name in ('packaging', 'synthetic-helper'):
        runtime.dependencies[name] = add_distribution(runtime.site, name)
    shutil.copyfile(Path(__file__).with_name('fixtures') / 'verified_protocol_worker.py', runtime.worker)
    runtime.worker.with_name('recovery_support.py').write_text(
        '# Purpose-written synthetic companion identity; no supplied source.\n', encoding='utf-8')
    (runtime.model / 'vocabulary.txt').write_bytes(b'Synthetic complete-pack identity only.')
    descriptor(runtime)
    return runtime


register = _base.register
