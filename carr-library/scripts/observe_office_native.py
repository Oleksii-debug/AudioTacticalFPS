"""Finite diagnostics of authored legacy input under the existing native owner.

This records observations; it is not a converter acceptance waiver. Diagnostic
variants do not change the product environment or its immutable runtime lease.
The crossed direct/worker matrix varies path length and Unicode independently.
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
import struct
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


def authored_subtree(element):
    """Bounded descendant evidence for the one actual rejected authored node."""
    rows, pending, encoded_bytes, clipped = [], [(element, ())], 0, False
    while pending and len(rows) < 64:
        node, path = pending.pop()
        row = {'path': list(path), 'tag': str(node.tag)[:512],
               'attributes': {str(key)[:512]: str(value)[:512]
                              for key, value in list(node.attrib.items())[:16]},
               'text': (node.text or '')[:512], 'tail': (node.tail or '')[:512],
               'children': len(node)}
        size = len(json.dumps(row, ensure_ascii=True).encode('ascii'))
        if encoded_bytes + size > 16 * 1024:
            clipped = True
            break
        encoded_bytes += size
        rows.append(row)
        clipped = clipped or (len(node.attrib) > 16
            or any(len(str(key)) > 512 or len(str(value)) > 512 for key, value in node.attrib.items())
            or len(node.text or '') > 512 or len(node.tail or '') > 512
            or len(node) > 16 or (len(path) >= 8 and len(node) > 0))
        if len(path) < 8:
            pending.extend((child, (*path, index)) for index, child in
                           reversed(list(enumerate(node, 1))[:16]))
    return {'nodes': rows, 'bounded': bool(pending) or clipped}


def failure_frames(error):
    """Record the actual rejecting authored node, without changing the parser."""
    rows, trace, count = [], error.__traceback__, 0
    while trace is not None and count < 32:
        frame = trace.tb_frame
        if (frame.f_globals.get('__name__') == 'carr_library.extract.document_formats_xml'
                and frame.f_code.co_name in ('_odf_blocks', '_odf_text', '_check_odf_visibility')):
            element = frame.f_locals.get('element')
            if element is not None:
                rows.append({'function': frame.f_code.co_name, 'line': trace.tb_lineno,
                    'tag': str(element.tag)[:512], 'subtree': authored_subtree(element),
                    'child_tags': [str(child.tag)[:512] for child in list(element)[:32]],
                    'children': [{'tag': str(child.tag)[:512],
                        'attributes': {str(key)[:512]: str(value)[:256]
                            for key, value in list(child.attrib.items())[:16]},
                        'text': (child.text or '')[:256], 'tail': (child.tail or '')[:256],
                        'child_tags': [str(item.tag)[:512] for item in list(child)[:16]]}
                        for child in list(element)[:16]],
                    'attribute_names': sorted(str(key)[:512] for key in element.attrib)[:32],
                    'attributes': {str(key)[:512]: str(value)[:256]
                                   for key, value in list(element.attrib.items())[:32]},
                    'text': (element.text or '')[:256], 'tail': (element.tail or '')[:256],
                    'path': list(frame.f_locals.get('path', ()))[:32]})
        trace, count = trace.tb_next, count + 1
    return rows


def odf_census(root, extension='odt'):
    """Read only this authored operation's bounded generated content.xml."""
    from defusedxml import ElementTree
    from carr_library.conversion.office import file_identity, regular_file, verified_reader
    assert extension in ('odt', 'odp', 'ods')
    output = root / ('office-output/source.' + extension)
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
    counts, nodes, visibility = Counter(), 0, []
    for node in tree.iter():
        nodes += 1
        if nodes > 4096:
            raise ValueError('Authored XML node census exceeded its bound.')
        counts[str(node.tag)] += 1
        if any(key.rsplit('}', 1)[-1] in ('display', 'visibility')
               for key in node.attrib) and len(visibility) < 64:
            visibility.append({'tag': str(node.tag)[:512],
                'attributes': {str(key)[:512]: str(value)[:256]
                               for key, value in list(node.attrib.items())[:32]},
                'child_tags': [str(child.tag)[:512] for child in list(node)[:32]],
                'text': (node.text or '')[:256]})
        if len(counts) > 256:
            raise ValueError('Authored XML tag census exceeded its bound.')
    office = '{urn:oasis:names:tc:opendocument:xmlns:office:1.0}'
    body = tree.find(office + 'body')
    rows, pending = [], [] if body is None else [(body, ())]
    while pending and len(rows) < 128:
        node, path = pending.pop()
        rows.append({'path': list(path), 'tag': str(node.tag)[:512],
                     'child_tags': [str(child.tag)[:512] for child in list(node)[:32]],
                     'attribute_names': sorted(str(key)[:512] for key in node.attrib)[:32],
                     'attributes': {str(key)[:512]: str(value)[:256]
                                    for key, value in list(node.attrib.items())[:32]},
                     'text': (node.text or '')[:256], 'tail': (node.tail or '')[:256],
                     'form_flags': {key: str(node.get(
                         '{urn:oasis:names:tc:opendocument:xmlns:form:1.0}' + key))[:32]
                         for key in ('apply-design-mode', 'automatic-focus')
                         if node.get('{urn:oasis:names:tc:opendocument:xmlns:form:1.0}' + key) is not None}})
        if len(path) < 8:
            pending.extend((child, (*path, index)) for index, child in
                           reversed(list(enumerate(node, 1))))
    return {'present': True, 'archive_bytes': info.st_size,
            'content_bytes': len(raw), 'content_sha256': sha256(raw).hexdigest(),
            'tag_counts': dict(sorted(counts.items())), 'body_nodes': rows,
            'visibility_nodes': visibility,
            'body_census_bounded': bool(pending)}


def observations(root, extension='odt'):
    result = {'stage_path': str(root), 'stage_length': len(str(root))}
    for key, operation in (('stage_files', namespace),
                           ('odf_census', lambda path: odf_census(path, extension))):
        try:
            result[key] = operation(root)
        except Exception as error:
            result[key] = {'observation_error': (type(error).__name__ + ': ' + str(error))[:2048]}
    return result


def child(mode, root, kind='doc'):
    from carr_library.conversion import office
    from carr_library.conversion.models import ConversionBudget
    assert mode in MODES and root.is_dir() and kind in ('doc', 'ppt', 'xls', 'wri')
    selected = json.loads((root / 'selection.json').read_text('utf-8'))
    source = root / ('source.' + kind)
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
    result = {'mode': mode, 'kind': kind, 'status': 'OBSERVED_FAILURE'}
    try:
        engine, separator, records, warnings = office.office_records(
            source, source.read_bytes(), '.' + kind, budget, deadline, selected)
        text = separator.join(item['raw_text'] for item in records)
        assert TEXT in text, 'Authored text did not survive conversion.'
        result.update(status='OBSERVED_SUCCESS', engine=engine,
                      text_sha256=sha256(text.encode()).hexdigest(), records=len(records))
    except BaseException as exc:
        result['error'] = (type(exc).__name__ + ': ' + str(exc))[:8192]
        result['rejected_parser_frames'] = failure_frames(exc)
    finally:
        assert sha256(source.read_bytes()).hexdigest() == before
        office._admit(selected, '.' + kind, monotonic() + 20)
        result.update(observations(root, {'doc': 'odt', 'ppt': 'odp', 'xls': 'ods', 'wri': 'odt'}[kind]))
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


def worker_observation(stage, selected, native, time_left, kind='doc'):
    """Use the real isolated worker, request, environment and process owner."""
    from carr_library.audio.process import run_process
    from carr_library.conversion import ConversionBudget, ConversionService
    from carr_library.conversion.service import _read_bytes
    from carr_library.conversion.office import MAX_STAGE_ENTRIES, PROFILE_BYTES
    budget = ConversionBudget(timeout=min(60, time_left), memory_bytes=4 * 1024**3)
    assert kind in ('doc', 'ppt', 'xls', 'wri')
    source, output, report = stage / ('source.' + kind), stage / 'reading.txt', stage / 'details.json'
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
        assert details['engine'] == 'selected-libreoffice-' + kind
        assert details['metadata']['selected_components_sha256'] == sha256(
            json.dumps(selected, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return outcome



def authored_write_with_fod(text):
    """One bounded text run and its required character/paragraph FOD pages.

    Layout follows the actual libwps MSWriteParser readFIB/readFOD/readStructures
    entry fields, mirrored at immutable a5ad3a399e52ff174e188edfca170a7689232d0b
    in deepin-community/libwps/src/lib/MSWrite.cpp. This is authored fixture data,
    with no embedded object, macro, nondefault property or third-party document.
    """
    body = text.encode('cp1252') + b'\r\n'
    assert 0 < len(body) <= 1024
    end = 128 + len(body)
    character_page = (end + 127) // 128
    paragraph_page = character_page + 1
    page_count = paragraph_page + 1
    header = bytearray(128)
    header[:6] = b'\x31\xbe\x00\x00\x00\xab'
    struct.pack_into('<I', header, 14, end)
    struct.pack_into('<H', header, 18, paragraph_page)
    for offset in (20, 22, 24, 26, 28, 96):
        struct.pack_into('<H', header, offset, page_count)
    fod = bytearray(128)
    struct.pack_into('<IIH', fod, 0, 128, end, 0xffff)
    fod[127] = 1
    result = (bytes(header) + body).ljust(character_page * 128, b'\0')
    result += bytes(fod) + bytes(fod)
    assert len(result) == page_count * 128
    return result


def format_census_only():
    """Six actual Linux observations; reuse the current bounded child helpers."""
    assert sys.platform == 'linux'
    from carr_library.audio.models import AudioBudget
    from carr_library.audio.process import run_process
    from carr_library.conversion.office import OfficeSelection, _admit
    native = Path(os.environ['CARR_SOFFICE']).resolve(strict=True)
    installed = identities()
    for row in installed.values():
        assert 'site-packages' in row['path'].replace('\\', '/').casefold()
    fixture_path = Path(__file__).resolve().parents[1] / 'tests/conversion/office_native_acceptance.py'
    fixture_before = sha256(fixture_path.read_bytes()).hexdigest()
    spec = importlib.util.spec_from_file_location('office_authored_fixture', fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    selected = OfficeSelection.capture(native).snapshot()
    with TemporaryDirectory(prefix='carr-office-format-observation-') as temporary:
        root = Path(temporary).resolve()
        deadline = monotonic() + 300
        xls = fixture.legacy_office(root, 'xls', TEXT)
        xls_bytes = xls.read_bytes()
        inputs = (('xls', 'actual_failed_format', xls_bytes, 'ods'),
                  ('wri', 'header_only', fixture.microsoft_write(TEXT), 'odt'),
                  ('wri', 'one_run_fod', authored_write_with_fod(TEXT), 'odt'))
        print(json.dumps({'status': 'AUTHORED_FORMATS_READY', 'installed': installed,
            'selection_sha256': sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()}),
            flush=True)
        for kind, variant, captured, extension in inputs:
            for route in ('direct', 'worker'):
                remaining = deadline - monotonic()
                if remaining < 1:
                    raise TimeoutError('The bounded Linux format census did not finish.')
                stage = root / (kind + '-' + variant + '-' + route)
                stage.mkdir()
                source = stage / ('source.' + kind)
                source.write_bytes(captured)
                (stage / 'selection.json').write_text(json.dumps(selected), encoding='utf-8')
                if route == 'worker':
                    outcome = worker_observation(stage, selected, native, remaining, kind)
                else:
                    outcome = run_process([sys.executable, '-I', '-B', '-X', 'utf8',
                        str(Path(__file__).resolve()), '--format-child', kind, str(stage)],
                        budget=AudioBudget(timeout=min(90, remaining),
                            max_output_bytes=128 * 1024**2, max_staging_files=4096),
                        owned_tree=True, cwd=stage, watch=(stage,), allow_failure=True)
                assert source.read_bytes() == captured and xls.read_bytes() == xls_bytes
                _admit(selected, '.' + kind, monotonic() + 20)
                print(json.dumps({'case': 'linux_actual_format', 'kind': kind,
                    'variant': variant, 'route': route, 'exit': outcome.returncode,
                    'source_bytes': len(captured), 'source_sha256': sha256(captured).hexdigest(),
                    'source_header_hex': captured[:128].hex(),
                    'stdout': outcome.stdout.decode('utf-8', 'replace')[:65536],
                    'stderr': outcome.stderr.decode('utf-8', 'replace')[:8192],
                    **observations(stage, extension)}, ensure_ascii=True), flush=True)
    assert identities() == installed
    assert sha256(fixture_path.read_bytes()).hexdigest() == fixture_before
    print('OBSERVATIONS_COMPLETE: six actual Linux routes; exact installed sources and inputs unchanged',
          flush=True)

def main():
    if sys.argv[1:] == ['--formats-only']:
        format_census_only()
        return
    if len(sys.argv) == 4 and sys.argv[1] == '--format-child':
        kind, root = sys.argv[2], Path(sys.argv[3]).resolve(strict=True)
        child('original', root, kind)
        return
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
        # These are the exact two new failures from b398 Windows acceptance.
        # Keep all earlier matrix cases; only capture finite authored ODP/ODS
        # structure. No generated content is admitted or published by this probe.
        for kind, extension in (('ppt', 'odp'), ('xls', 'ods')):
            original_format = fixture.legacy_office(root, kind, TEXT)
            format_bytes = original_format.read_bytes()
            for route in ('direct', 'worker'):
                remaining = diagnostic_deadline - monotonic()
                if remaining < 1:
                    raise TimeoutError('The bounded Office format census did not finish.')
                stage = matrix_stage(root, kind + '-' + route[0], short_length, False)
                source = stage / ('source.' + kind)
                source.write_bytes(format_bytes)
                (stage / 'selection.json').write_text(json.dumps(selected), encoding='utf-8')
                if route == 'worker':
                    outcome = worker_observation(stage, selected, native, remaining, kind)
                else:
                    outcome = run_process([sys.executable, '-I', '-B', '-X', 'utf8',
                        str(Path(__file__).resolve()), '--format-child', kind, str(stage)],
                        budget=AudioBudget(timeout=min(90, remaining),
                            max_output_bytes=128 * 1024**2, max_staging_files=4096),
                        owned_tree=True, cwd=stage, watch=(stage,), allow_failure=True)
                assert source.read_bytes() == original_format.read_bytes() == format_bytes
                from carr_library.conversion.office import _admit
                _admit(selected, '.' + kind, monotonic() + 20)
                print(json.dumps({'case': 'actual_failed_format', 'kind': kind,
                    'route': route, 'exit': outcome.returncode,
                    'source_bytes': len(format_bytes),
                    'source_sha256': sha256(format_bytes).hexdigest(),
                    'stdout': outcome.stdout.decode('utf-8', 'replace')[:65536],
                    'stderr': outcome.stderr.decode('utf-8', 'replace')[:8192],
                    **observations(stage, extension)}, ensure_ascii=True), flush=True)
        # Keep the actual failing header-only input as a negative observation.
        # Compare only the two required one-run FOD pages, with the exact same
        # selected MS_Write import filter, profile, worker and process owner.
        for variant, format_bytes in (
                ('header_only', fixture.microsoft_write(TEXT)),
                ('one_run_fod', authored_write_with_fod(TEXT))):
            for route in ('direct', 'worker'):
                remaining = diagnostic_deadline - monotonic()
                if remaining < 1:
                    raise TimeoutError('The bounded Microsoft Write census did not finish.')
                stage = matrix_stage(root, 'wri-' + variant + '-' + route[0],
                                     max(short_length, len(str(root)) + 48), False)
                source = stage / 'source.wri'
                source.write_bytes(format_bytes)
                (stage / 'selection.json').write_text(json.dumps(selected), encoding='utf-8')
                if route == 'worker':
                    outcome = worker_observation(stage, selected, native, remaining, 'wri')
                else:
                    outcome = run_process([sys.executable, '-I', '-B', '-X', 'utf8',
                        str(Path(__file__).resolve()), '--format-child', 'wri', str(stage)],
                        budget=AudioBudget(timeout=min(90, remaining),
                            max_output_bytes=128 * 1024**2, max_staging_files=4096),
                        owned_tree=True, cwd=stage, watch=(stage,), allow_failure=True)
                assert source.read_bytes() == format_bytes
                from carr_library.conversion.office import _admit
                _admit(selected, '.wri', monotonic() + 20)
                print(json.dumps({'case': 'actual_write_fixture', 'variant': variant,
                    'route': route, 'exit': outcome.returncode,
                    'source_bytes': len(format_bytes),
                    'source_sha256': sha256(format_bytes).hexdigest(),
                    'source_header_hex': format_bytes[:128].hex(),
                    'stdout': outcome.stdout.decode('utf-8', 'replace')[:65536],
                    'stderr': outcome.stderr.decode('utf-8', 'replace')[:8192],
                    **observations(stage, 'odt')}, ensure_ascii=True), flush=True)
    assert identities() == installed
    assert sha256(fixture_path.read_bytes()).hexdigest() == fixture_before
    print('OBSERVATIONS_COMPLETE: exact installed sources and authored input unchanged', flush=True)


if __name__ == '__main__':
    main()
