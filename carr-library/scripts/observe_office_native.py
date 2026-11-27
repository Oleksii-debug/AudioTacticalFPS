"""Finite diagnostics of authored legacy input under the existing native owner.

This records observations; it is not a converter acceptance waiver. Diagnostic
variants do not change the product environment or its immutable runtime lease.
"""
from contextlib import contextmanager
from collections import Counter
from dataclasses import asdict, replace
from hashlib import sha256
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from zipfile import ZipFile

MODES = ('original', 'windows_known_dirs', 'console_handles', 'without_component_lease')
KEYS = ('USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA', 'ALLUSERSPROFILE',
        'SystemDrive', 'COMSPEC', 'ProgramFiles', 'ProgramFiles(x86)', 'CommonProgramFiles')
MODULES = ('carr_library.conversion.office', 'carr_library.audio.process',
           'carr_library.audio.windows_job', 'carr_library.conversion.worker',
           'carr_library.conversion.service', 'carr_library.extract.document_formats_xml')
TEXT = 'Authored selected office diagnostic text.'


def identities():
    return {name: {'path': str(Path(importlib.import_module(name).__file__).resolve()),
                   'sha256': sha256(Path(importlib.import_module(name).__file__).read_bytes()).hexdigest()}
            for name in MODULES}


def namespace(root):
    rows, pending, count = [], [(root, 0)], 0
    while pending:
        directory, depth = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > 256 or depth > 16:
                    rows.append({'bounded': True, 'entries': count})
                    return rows
                path = Path(entry.path)
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400:
                    pending.append((path, depth + 1))
                else:
                    rows.append({'name': str(path.relative_to(root)), 'bytes': info.st_size,
                                 'absolute_length': len(str(path)),
                                 'regular': stat.S_ISREG(info.st_mode),
                                 'reparse': bool(getattr(info, 'st_file_attributes', 0) & 0x400)})
    return rows


def failure_frames(error):
    """Record the actual rejecting authored node, without changing the parser."""
    rows, trace, count = [], error.__traceback__, 0
    while trace is not None and count < 32:
        frame = trace.tb_frame
        if (frame.f_globals.get('__name__') == 'carr_library.extract.document_formats_xml'
                and frame.f_code.co_name in ('_odf_blocks', '_odf_text')):
            element = frame.f_locals.get('element')
            if element is not None:
                rows.append({'function': frame.f_code.co_name, 'line': trace.tb_lineno,
                    'tag': str(element.tag)[:512],
                    'child_tags': [str(child.tag)[:512] for child in list(element)[:32]],
                    'attribute_names': sorted(str(key)[:512] for key in element.attrib)[:32],
                    'path': list(frame.f_locals.get('path', ()))[:32]})
        trace, count = trace.tb_next, count + 1
    return rows


def odf_census(root):
    """Read only this authored operation's bounded generated content.xml."""
    from defusedxml import ElementTree
    from carr_library.conversion.office import file_identity, regular_file, verified_reader
    output = root / 'office-output/source.odt'
    if not output.exists():
        return {'present': False}
    info = regular_file(output)
    if info.st_nlink != 1 or not 0 < info.st_size <= 4 * 1024**2:
        raise ValueError('Authored intermediate exceeds the diagnostic census bound.')
    with verified_reader(output, expected_identity=file_identity(info)) as stream:
        with ZipFile(stream) as archive:
            members = archive.infolist()
            if len(members) > 256:
                raise ValueError('Authored intermediate member inventory exceeded its bound.')
            choices = [member for member in members if member.filename == 'content.xml']
            if len(choices) != 1 or not 0 < choices[0].file_size <= 256 * 1024:
                raise ValueError('Authored content.xml is missing, duplicated or over its diagnostic bound.')
            with archive.open(choices[0]) as content:
                raw = content.read(256 * 1024 + 1)
            if len(raw) != choices[0].file_size:
                raise ValueError('Authored content.xml changed during its bounded capture.')
    tree = ElementTree.fromstring(raw)
    counts, nodes = Counter(), 0
    for node in tree.iter():
        nodes += 1
        if nodes > 4096:
            raise ValueError('Authored XML node census exceeded its bound.')
        counts[str(node.tag)] += 1
        if len(counts) > 256:
            raise ValueError('Authored XML tag census exceeded its bound.')
    office = '{urn:oasis:names:tc:opendocument:xmlns:office:1.0}'
    body = tree.find(office + 'body')
    rows, pending = [], [] if body is None else [(body, ())]
    while pending and len(rows) < 128:
        node, path = pending.pop()
        rows.append({'path': list(path), 'tag': str(node.tag)[:512],
                     'child_tags': [str(child.tag)[:512] for child in list(node)[:32]],
                     'attribute_names': sorted(str(key)[:512] for key in node.attrib)[:32]})
        if len(path) < 8:
            pending.extend((child, (*path, index)) for index, child in
                           reversed(list(enumerate(node, 1))))
    return {'present': True, 'archive_bytes': info.st_size,
            'content_bytes': len(raw), 'content_sha256': sha256(raw).hexdigest(),
            'tag_counts': dict(sorted(counts.items())), 'body_nodes': rows,
            'body_census_bounded': bool(pending)}


def observations(root):
    result = {'stage_path': str(root), 'stage_length': len(str(root))}
    for key, operation in (('stage_files', namespace), ('odf_census', odf_census)):
        try:
            result[key] = operation(root)
        except Exception as error:
            result[key] = {'observation_error': (type(error).__name__ + ': ' + str(error))[:2048]}
    return result


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
        text = separator.join(item['raw_text'] for item in records)
        assert TEXT in text, 'Authored text did not survive conversion.'
        result.update(status='OBSERVED_SUCCESS', engine=engine,
                      text_sha256=sha256(text.encode()).hexdigest(), records=len(records))
    except BaseException as exc:
        result['error'] = (type(exc).__name__ + ': ' + str(exc))[:8192]
        result['rejected_parser_frames'] = failure_frames(exc)
    finally:
        assert sha256(source.read_bytes()).hexdigest() == before
        office._admit(selected, '.doc', monotonic() + 20)
        result.update(observations(root))
        result['source_sha256'] = before
        print(json.dumps(result, ensure_ascii=True), flush=True)


def matrix_stage(root, label, length, unicode):
    # Different leaf labels keep each run independent; the crossed cases have
    # equal Windows character lengths, with only the declared factor changed.
    prefix = label + '-' + ('копія' if unicode else 'copyx') + '-'
    count = length - len(str(root)) - 1 - len(prefix)
    if not 0 <= count <= 128:
        raise ValueError('The diagnostic root cannot fit its declared path comparison.')
    stage = root / (prefix + 'x' * count)
    stage.mkdir()
    assert len(str(stage)) == length
    return stage


def worker_observation(stage, selected, native, time_left):
    """Use the real isolated worker, request, environment and process owner."""
    from carr_library.audio.process import run_process
    from carr_library.conversion import ConversionBudget, ConversionService
    from carr_library.conversion.service import _read_bytes
    from carr_library.conversion.office import MAX_STAGE_ENTRIES, PROFILE_BYTES
    budget = ConversionBudget(timeout=min(60, time_left), memory_bytes=4 * 1024**3)
    source, output, report = stage / 'source.doc', stage / 'reading.txt', stage / 'details.json'
    service = ConversionService(stage, input_roots=(source,), office=native, budget=budget)
    assert service.office.snapshot() == selected
    request = stage / 'request.json'
    request.write_text(json.dumps({'source': str(source), 'output': str(output),
        'report': str(report), 'budget': asdict(budget), 'operation': 'office',
        'office': selected}, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    process_budget = replace(service._process_budget(),
        max_output_bytes=2 * budget.max_source_bytes + budget.max_text_bytes + PROFILE_BYTES,
        max_staging_files=MAX_STAGE_ENTRIES)
    outcome = run_process([str(Path(sys.executable).absolute()), '-I', '-B', '-m',
        'carr_library.conversion.worker', str(request)], budget=process_budget,
        watch=(stage,), env=service._env(stage), cwd=stage, owned_tree=True, allow_failure=True)
    service.office._unchanged()
    if outcome.returncode == 0:
        payload = _read_bytes(output, budget.max_text_bytes)
        details = json.loads(_read_bytes(report, 16 * 1024**2).decode('utf-8'))
        assert TEXT in payload.decode('utf-8')
        assert details['engine'] == 'selected-libreoffice-doc'
        assert details['metadata']['selected_components_sha256'] == sha256(
            json.dumps(selected, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return outcome


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
        diagnostic_deadline = monotonic() + 480
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
        short_length = max(96, len(str(root)) + 20)
        long_length = max(139, short_length + 43)
        for layout, length, unicode in (
                ('short_ascii', short_length, False),
                ('short_unicode', short_length, True),
                ('deep_ascii', long_length, False),
                ('deep_unicode', long_length, True)):
            for route in ('direct', 'worker'):
                time_left = diagnostic_deadline - monotonic()
                if time_left < 1:
                    raise TimeoutError('The bounded Office diagnostic matrix did not finish.')
                label = ('d' if route == 'direct' else 'w') + (
                    's' if layout.startswith('short') else 'l') + ('u' if unicode else 'a')
                stage = matrix_stage(root, label, length, unicode)
                (stage / 'source.doc').write_bytes(captured)
                (stage / 'selection.json').write_text(json.dumps(selected), encoding='utf-8')
                before = sha256((stage / 'source.doc').read_bytes()).hexdigest()
                if route == 'worker':
                    outcome = worker_observation(stage, selected, native, time_left)
                else:
                    outcome = run_process([sys.executable, '-I', '-B', '-X', 'utf8',
                        str(Path(__file__).resolve()), '--child', 'original', str(stage)],
                        budget=AudioBudget(timeout=min(90, time_left),
                            max_output_bytes=128 * 1024**2, max_staging_files=4096),
                        owned_tree=True, cwd=stage, watch=(stage,), allow_failure=True)
                assert sha256((stage / 'source.doc').read_bytes()).hexdigest() == before
                from carr_library.conversion.office import _admit
                _admit(selected, '.doc', monotonic() + 20)
                print(json.dumps({'case': layout, 'route': route, 'exit': outcome.returncode,
                    'stdout': outcome.stdout.decode('utf-8', 'replace')[:32768],
                    'stderr': outcome.stderr.decode('utf-8', 'replace')[:8192],
                    **observations(stage)}, ensure_ascii=True), flush=True)
                assert original.read_bytes() == captured
    assert identities() == installed
    assert sha256(fixture_path.read_bytes()).hexdigest() == fixture_before
    print('OBSERVATIONS_COMPLETE: exact installed sources and authored input unchanged', flush=True)


if __name__ == '__main__':
    main()
