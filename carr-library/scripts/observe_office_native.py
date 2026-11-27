"""Finite diagnostics of authored legacy input under the existing native owner.

This records observations; it is not a converter acceptance waiver. Diagnostic
variants do not change the product environment or its immutable runtime lease.
"""
from contextlib import contextmanager
from hashlib import sha256
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from time import monotonic

MODES = ('original', 'windows_known_dirs', 'console_handles', 'without_component_lease')
KEYS = ('USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA', 'ALLUSERSPROFILE',
        'SystemDrive', 'COMSPEC', 'ProgramFiles', 'ProgramFiles(x86)', 'CommonProgramFiles')
MODULES = ('carr_library.conversion.office', 'carr_library.audio.process',
           'carr_library.audio.windows_job', 'carr_library.conversion.worker')
TEXT = 'Authored selected office diagnostic text.'


def identities():
    return {name: {'path': str(Path(importlib.import_module(name).__file__).resolve()),
                   'sha256': sha256(Path(importlib.import_module(name).__file__).read_bytes()).hexdigest()}
            for name in MODULES}


def namespace(root):
    rows = []
    for directory, dirs, files in os.walk(root):
        if len(rows) + len(dirs) + len(files) > 256:
            rows.append({'bounded': True})
            break
        rows.extend({'name': str((Path(directory) / n).relative_to(root)),
                     'bytes': (Path(directory) / n).stat().st_size} for n in files)
    return rows


def child(mode, root):
    from carr_library.conversion import office
    from carr_library.conversion.models import ConversionBudget
    assert mode in MODES and root.is_dir()
    selected = json.loads((root / 'selection.json').read_text('utf-8'))
    source = root / 'source.doc'
    before = sha256(source.read_bytes()).hexdigest()
    inherited = {key: os.environ[key] for key in KEYS if key in os.environ}
    original_popen = office.subprocess.Popen
    def observed_popen(args, *pos, **kw):
        assert str(args[0]) == str(Path(selected['root']) / selected['executable'])
        if mode == 'windows_known_dirs':
            kw['env'] = dict(kw['env'], **inherited)
        if mode == 'console_handles':
            kw['creationflags'] = 0
        print(json.dumps({'event': 'spawn', 'mode': mode, 'args': [str(x) for x in args],
                          'environment_keys': sorted(kw['env']),
                          'creationflags': kw.get('creationflags')}, ensure_ascii=True), flush=True)
        return original_popen(args, *pos, **kw)
    office.subprocess.Popen = observed_popen
    if mode == 'without_component_lease':
        @contextmanager
        def observed_lease(selection):
            # Only this authored diagnostic changes the lease. Full before/after
            # component admission and the parent process owner remain in force.
            yield
        office._runtime_lease = observed_lease
    budget = ConversionBudget(timeout=60, memory_bytes=4 * 1024**3)
    deadline = monotonic() + budget.timeout
    result = {'mode': mode, 'status': 'OBSERVED_FAILURE'}
    try:
        engine, separator, records, warnings = office.office_records(
            source, source.read_bytes(), '.doc', budget, deadline, selected)
        text = separator.join(item['text'] for item in records)
        assert TEXT in text, 'Authored text did not survive conversion.'
        result.update(status='OBSERVED_SUCCESS', engine=engine,
                      text_sha256=sha256(text.encode()).hexdigest(), records=len(records))
    except BaseException as exc:
        result['error'] = (type(exc).__name__ + ': ' + str(exc))[:8192]
    finally:
        assert sha256(source.read_bytes()).hexdigest() == before
        office._admit(selected, '.doc', monotonic() + 20)
        result['stage_files'] = namespace(root)
        result['source_sha256'] = before
        print(json.dumps(result, ensure_ascii=True), flush=True)


def main():
    if len(sys.argv) == 4 and sys.argv[1] == '--child':
        mode, root = sys.argv[2], Path(sys.argv[3]).resolve(strict=True)
        child(mode, root)
        return
    assert sys.platform == 'win32', 'This diagnostic is Windows-specific.'
    from carr_library.audio.models import AudioBudget
    from carr_library.audio.process import run_process
    from carr_library.conversion.office import OfficeSelection
    native = Path(os.environ['CARR_SOFFICE']).resolve(strict=True)
    installed = identities()
    for row in installed.values():
        assert 'site-packages' in row['path'].replace('\\', '/').casefold()
    fixture_path = Path(__file__).resolve().parents[1] / 'tests/conversion/office_native_acceptance.py'
    fixture_before = sha256(fixture_path.read_bytes()).hexdigest()
    spec = importlib.util.spec_from_file_location('office_authored_fixture', fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    with TemporaryDirectory(prefix='carr-office-observation-') as temporary:
        root = Path(temporary).resolve()
        original = fixture.legacy_office(root, 'doc', TEXT)
        captured = original.read_bytes()
        selected = OfficeSelection.capture(native).snapshot()
        print(json.dumps({'status': 'AUTHORED_INPUT_READY', 'bytes': len(captured),
                          'sha256': sha256(captured).hexdigest(), 'installed': installed,
                          'selection_sha256': sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()}),
              flush=True)
        for mode in MODES:
            stage = root / mode
            stage.mkdir()
            (stage / 'source.doc').write_bytes(captured)
            (stage / 'selection.json').write_text(json.dumps(selected), encoding='utf-8')
            outcome = run_process([sys.executable, '-I', '-B', '-X', 'utf8',
                str(Path(__file__).resolve()), '--child', mode, str(stage)],
                budget=AudioBudget(timeout=90, max_output_bytes=128 * 1024**2,
                                   max_staging_files=4096),
                owned_tree=True, cwd=stage, watch=(stage,), allow_failure=True)
            print(json.dumps({'mode': mode, 'exit': outcome.returncode,
                              'stdout': outcome.stdout.decode('utf-8', 'replace')[:32768],
                              'stderr': outcome.stderr.decode('utf-8', 'replace')[:8192]}), flush=True)
            assert original.read_bytes() == captured
    assert identities() == installed
    assert sha256(fixture_path.read_bytes()).hexdigest() == fixture_before
    print('OBSERVATIONS_COMPLETE: exact installed sources and authored input unchanged', flush=True)


if __name__ == '__main__':
    main()
