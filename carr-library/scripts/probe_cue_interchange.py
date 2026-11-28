"""Read actual published CUE bytes with pinned upstream libcue; no GUI claim."""
import argparse
import ctypes
from hashlib import sha256
import importlib.util
import json
import os
import subprocess
import sysconfig
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from carr_library.studio.interchange import chapter_cues
from carr_library.studio.render import ProductionService


UPSTREAM = 'cfb98a060fd79dbc3463d85f0f29c3c335dfa0ea'


def run(library, report):
    if not sys.flags.isolated:
        raise RuntimeError('Use the isolated installed interpreter for the independent reader.')
    library = Path(library).resolve(strict=True)
    if not library.is_file() or library.is_symlink():
        raise ValueError('Choose the actual built libcue shared library.')
    root = Path(__file__).resolve().parents[1]
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, check=True,
                            capture_output=True, text=True, timeout=10).stdout.strip()
    assert commit == os.environ['CARR_CUE_HEAD']
    source = root / 'src' / 'carr_library'
    installed = Path(sysconfig.get_path('purelib')) / 'carr_library'
    import carr_library
    assert Path(carr_library.__file__).resolve() == installed.resolve() / '__init__.py'
    def inventory(directory):
        return {path.relative_to(directory).as_posix(): sha256(path.read_bytes()).hexdigest()
                for path in sorted(directory.rglob('*.py')) if path.is_file() and not path.is_symlink()}
    expected = inventory(source)
    assert expected and inventory(installed) == expected
    library_hash = sha256(library.read_bytes()).hexdigest()
    helper_hash = sha256(Path(__file__).read_bytes()).hexdigest()
    fixture_path = root / 'tests' / 'studio' / 'test_interchange.py'
    fixture_bytes = fixture_path.read_bytes()
    spec = importlib.util.spec_from_file_location('cue_acceptance_fixture', fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    native = ctypes.CDLL(str(library))
    native.cue_parse_string.argtypes = [ctypes.c_char_p]
    native.cue_parse_string.restype = ctypes.c_void_p
    native.cd_delete.argtypes = [ctypes.c_void_p]
    native.cd_delete.restype = None
    native.cd_get_ntrack.argtypes = [ctypes.c_void_p]
    native.cd_get_ntrack.restype = ctypes.c_int
    native.cd_get_track.argtypes = [ctypes.c_void_p, ctypes.c_int]
    native.cd_get_track.restype = ctypes.c_void_p
    native.track_get_filename.argtypes = [ctypes.c_void_p]
    native.track_get_filename.restype = ctypes.c_char_p
    native.track_get_start.argtypes = [ctypes.c_void_p]
    native.track_get_start.restype = ctypes.c_long
    evidence = []

    def consume(name, payload, expected):
        disc = native.cue_parse_string(payload)
        if not disc:
            raise AssertionError('Pinned libcue refused generated CUE bytes.')
        try:
            count = native.cd_get_ntrack(disc)
            assert count == len(expected) and 1 <= count <= 99
            actual = []
            for number, wanted in enumerate(expected, 1):
                track = native.cd_get_track(disc, number)
                assert track and native.track_get_filename(track) == b'master.wav'
                index = native.track_get_start(track)
                assert index == wanted['index_75hz'], (name, number, index, wanted)
                actual.append(index)
            evidence.append({'file': name, 'sha256': sha256(payload).hexdigest(),
                             'tracks': count, 'indices_75hz': actual})
        finally:
            native.cd_delete(disc)

    with TemporaryDirectory(prefix='carr-cue-upstream-') as temporary:
        work = Path(temporary)
        output = work / 'outputs'; output.mkdir()
        service = ProductionService(work / 'library' / 'state' / 'studio', voices=fixture.ToneVoices())
        script = '[Scene: Архів]\nNarrator: Exact original.\n[Scene: Žilina]\nNarrator: Exact second line.\n'
        project = service.create(script, title='Actual UTF-8 upstream CUE acceptance', voice_ids=('tone',))
        identity = service.submit(project, output, stems=True)
        state = service.run(identity)
        assert state['status'] == 'complete'
        before = {item['path']: sha256(Path(item['path']).read_bytes()).hexdigest()
                  for item in state['artifacts']}
        destination = Path(state['destination'])
        for sheet in state['result']['editor_interchange']['files']:
            cue = destination / sheet['file']
            consume(sheet['file'], cue.read_bytes(), sheet['tracks'])
        assert before == {path: sha256(Path(path).read_bytes()).hexdigest() for path in before}
        assert state == service.get(identity)
        assert json.loads((destination / 'project.json').read_text(encoding='utf-8'))['original_script'] == script
        production = {'job': identity, 'project_fingerprint': project.fingerprint,
                      'original_script_sha256': sha256(script.encode()).hexdigest(),
                      'master_sha256': before[str(destination / 'master.wav')],
                      'artifact_count': len(before), 'artifact_bytes_unchanged': True}
        assert service.close(), 'Actual interchange producer must release its retained resources.'

    # The sheet boundary and long absolute offsets are real upstream parser calls,
    # with simulated measured timelines; they are not long audio render claims.
    project, timeline = fixture.measured(100, 3601.5)
    artifacts, result = chapter_cues(project, timeline)
    for (name, payload), sheet in zip(artifacts, result['files']):
        consume('synthetic-boundary/' + name, payload, sheet['tracks'])
    assert fixture_path.read_bytes() == fixture_bytes
    assert inventory(source) == expected and inventory(installed) == expected
    assert subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, check=True,
                          capture_output=True, text=True, timeout=10).stdout.strip() == commit
    assert sha256(library.read_bytes()).hexdigest() == library_hash
    assert sha256(Path(__file__).read_bytes()).hexdigest() == helper_hash
    value = {'schema': 1, 'status': 'PASS', 'commit': commit,
        'package_python_files': len(expected),
        'package_inventory_sha256': sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest(),
        'upstream': 'lipnitsk/libcue',
        'upstream_commit': UPSTREAM, 'library_sha256': sha256(library.read_bytes()).hexdigest(),
        'fixture_sha256': sha256(fixture_bytes).hexdigest(),
        'probe_sha256': sha256(Path(__file__).read_bytes()).hexdigest(),
        'production': production, 'parsed_sheets': evidence,
        'actual_external_gui_editor_import': 'NOT_RUN', 'acoustic_quality': 'NOT_RUN',
        'physical_output': 'NOT_RUN', 'NVDA': 'NOT_RUN'}
    Path(report).write_text(json.dumps(value, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(value, sort_keys=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--libcue', required=True)
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    run(args.libcue, args.report)
