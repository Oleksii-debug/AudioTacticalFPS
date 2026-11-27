"""Bounded diagnostic reproduction of known synthetic native fixtures only.

This helper never changes converter qualification or handles user documents.
It substitutes only diagnostic capture around the same selected adapter call;
the existing owned-process runner settles descendants before held inputs and
the private fixture stage are released. Reproduction is not a test PASS.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import tempfile
import time


def diagnose_selected_fixture(family, source, executable, directory):
    from carr_library.audio.cleanup import AudioCleanupPending, wait_for_cleanup
    from carr_library.audio.recording_process import CaptureBudget, run_process
    from carr_library.conversion import ConversionBudget, ConversionError
    from carr_library.conversion import legacy, office
    from carr_library.conversion.legacy_runtime import ToolSelection

    if family not in ('office', 'calibre'):
        raise ValueError('Only the two fixed synthetic native reproductions are allowed.')
    source, executable, directory = Path(source), Path(executable), Path(directory)
    raw = source.read_bytes()
    if not 0 < len(raw) <= 64 * 1024**2:
        raise ValueError('Synthetic diagnostic source exceeds its input budget.')
    stage = Path(tempfile.mkdtemp(prefix='.carr-diagnostic-', dir=directory))
    module = office if family == 'office' else legacy
    previous = module._run
    deadline = time.monotonic() + 90

    def captured(args, selected_stage, operation_deadline, **options):
        environment = {key: value for key, value in os.environ.items()
                       if key in ('SYSTEMROOT', 'WINDIR', 'LANG')}
        environment['PATH'] = os.pathsep.join((str(Path(args[0]).parent),
            str(Path(environment.get('SYSTEMROOT', 'C:/Windows')) / 'System32')))
        environment.update(TMPDIR=str(selected_stage), TEMP=str(selected_stage), TMP=str(selected_stage))
        if family == 'office':
            environment.update(SAL_USE_VCLPLUGIN='svp', SAL_DISABLE_OPENCL='1', MAX_CONCURRENCY='1')
        else:
            environment['PYTHONDONTWRITEBYTECODE'] = '1'
            environment.update(options.get('environment') or {})
        remaining = min(deadline, operation_deadline) - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Synthetic native diagnostic deadline expired.')
        try:
            result = run_process([str(value) for value in args], cwd=selected_stage, env=environment,
                cancelled=lambda: time.monotonic() >= deadline, watch=(selected_stage,),
                budget=CaptureBudget(timeout=remaining, max_output_bytes=options.get('stage_limit', 192 * 1024**2),
                                     max_log_bytes=256 * 1024, max_staging_files=4096),
                allow_failure=True)
        except AudioCleanupPending as error:
            wait_for_cleanup(error)
            raise
        print('NATIVE_DIAGNOSTIC ' + json.dumps({
            'schema': 1, 'family': family, 'reproduction_only': True,
            'source_sha256': sha256(raw).hexdigest(),
            'adapter_module': str(Path(module.__file__).resolve()),
            'adapter_sha256': sha256(Path(module.__file__).read_bytes()).hexdigest(),
            'executable': str(executable), 'returncode': result.returncode,
            'elapsed_seconds': result.elapsed,
            'arguments': [str(value) for value in args],
            'stdout': result.stdout[-4096:].decode('utf-8', 'replace'),
            'stderr': result.stderr[-4096:].decode('utf-8', 'replace'),
        }, ensure_ascii=False, sort_keys=True), flush=True)
        if result.returncode:
            raise ConversionError('Synthetic selected native reproduction returned ' + str(result.returncode))
        return (result.stdout, bool(result.stderr)) if family == 'office' else result.stdout

    try:
        selected = stage / ('source' + source.suffix.casefold())
        selected.write_bytes(raw)
        budget = ConversionBudget(timeout=90)
        module._run = captured
        if family == 'office':
            selection = office.OfficeSelection.capture(executable).snapshot()
            result = office.office_records(selected, raw, selected.suffix, budget, deadline, selection)
        else:
            selection = ToolSelection.capture(executable, 'calibre').snapshot(stage)
            result = legacy._calibre(selected, raw, selected.suffix, budget, deadline, selection)
        print('NATIVE_DIAGNOSTIC ' + json.dumps({'family': family, 'reproduction_only': True,
            'adapter_result': result[0], 'records': len(result[2])}, sort_keys=True), flush=True)
    except Exception as error:
        names = sorted(str(path.relative_to(stage)) for path in (stage / 'office-output').glob('*'))[:32]
        print('NATIVE_DIAGNOSTIC ' + json.dumps({'family': family, 'reproduction_only': True,
            'error_type': type(error).__name__, 'error': str(error)[:1000],
            'intermediate_names': names}, sort_keys=True), flush=True)
    finally:
        module._run = previous
        shutil.rmtree(stage)
