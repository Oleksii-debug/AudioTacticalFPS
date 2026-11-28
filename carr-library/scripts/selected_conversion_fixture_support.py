"""Test-only selected-runtime fixture support; no downloader or product engine.

The caller's active external-address firewall covers every selected executable.
All native fixture construction uses the existing owned Windows Job and broker.
Public CHM/DjVu samples are pinned external CI inputs; generated inputs contain
only the exact synthetic string below. Neither fixture bytes nor runtimes belong
in a report-only artifact or application bundle.
"""
from contextlib import contextmanager, nullcontext
from hashlib import sha1, sha256
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import struct
import sys
from xml.sax.saxutils import escape

TEXT = 'Synthetic selected-format reading document. Exact source remains separate.'
MODULES = (
    'carr_library.application',
    'carr_library.catalog.intake',
    'carr_library.conversion.models',
    'carr_library.conversion.service',
    'carr_library.conversion.worker',
    'carr_library.conversion.formats',
    'carr_library.conversion._selected_files',
    'carr_library.conversion.calibre',
    'carr_library.conversion.office',
    'carr_library.conversion.selected_adapters',
    'carr_library.conversion.selected_worker',
    'carr_library.conversion.selected',
    'carr_library.extract.document_formats',
    'carr_library.extract.document_formats_xml',
    'carr_library.operations.broker',
    'carr_library.audio.cleanup',
    'carr_library.audio.process',
    'carr_library.audio.windows_job',
    'carr_library.file_publication',
    'carr_library.durable_state',
    'carr_library.backup_sidecars',
    'carr_library.speech._io',
)


def origins(source_root, mode):
    source_root = Path(source_root).resolve(strict=True)
    result = {}
    for name in MODULES:
        module = importlib.import_module(name)
        actual = Path(module.__file__).resolve(strict=True)
        expected = source_root.joinpath(*name.split('.')).with_suffix('.py')
        if sha256(actual.read_bytes()).hexdigest() != sha256(expected.read_bytes()).hexdigest():
            raise AssertionError('Selected native qualification imported different module bytes: ' + name)
        if mode == 'source':
            if actual != expected.resolve(strict=True):
                raise AssertionError('A source qualification imported another package: ' + name)
        elif (actual.is_relative_to(source_root.parent) or 'site-packages' not in actual.parts
              or not actual.is_relative_to(Path(sys.prefix).resolve(strict=True))):
            raise AssertionError('An installed qualification imported checkout code: ' + name)
        result[name] = {'path': str(actual), 'sha256': sha256(actual.read_bytes()).hexdigest()}
    return result


def load_lock(path, downloads):
    from carr_library.backup_sidecars import read_regular
    from carr_library.conversion._selected_files import regular_file, digest_file, file_identity
    raw = read_regular(path, 64 * 1024)
    lock = json.loads(raw)
    if (lock.get('schema') != 'carr-selected-document-runtime-lock/1'
            or [item.get('name') for item in lock.get('runtimes', [])] != ['calibre', 'libreoffice', 'pandoc']
            or len(lock.get('fixtures', [])) != 4):
        raise AssertionError('The exact selected-runtime lock is missing.')
    results = {}
    downloads = Path(downloads).resolve(strict=True)
    for item in lock['runtimes'] + lock['fixtures']:
        name = item.get('filename', item.get('name'))
        if Path(name).name != name:
            raise AssertionError('The locked external artifact must use one filename.')
        selected = downloads / name
        info = regular_file(selected)
        maximum = item.get('maximum_bytes', item.get('bytes', 0))
        if not 0 < info.st_size <= maximum or ('bytes' in item and info.st_size != item['bytes']):
            raise AssertionError('The selected external artifact has a different bounded size: ' + name)
        digest = digest_file(selected, expected_identity=file_identity(info))
        if 'sha256' in item and digest != item['sha256']:
            raise AssertionError('The official selected runtime hash differs: ' + name)
        if 'git_blob_sha1' in item:
            raw = read_regular(selected, maximum)
            if len(raw) != info.st_size or sha256(raw).hexdigest() != digest:
                raise AssertionError('The public fixture changed while checking: ' + name)
            blob = sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
            if blob != item['git_blob_sha1']:
                raise AssertionError('The pinned public fixture/notice differs: ' + name)
        results[name] = {'sha256': digest, 'bytes': info.st_size}
    return lock, {'lock_sha256': sha256(read_regular(path, 64 * 1024)).hexdigest(), 'artifacts': results}


def firewall(path, source_root, executables):
    """Reuse the current loopback gate's actual caller-owned receipt contract."""
    script = Path(source_root).parent / 'scripts' / 'translation_loopback_check.py'
    spec = importlib.util.spec_from_file_location('_selected_loopback_policy', script)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    boundary = helper.firewall_receipt(Path(path).resolve(strict=True))
    value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if set(value.get('enabled_profiles', [])) != {'Domain', 'Private', 'Public'}:
        raise AssertionError('The three active firewall profiles must be read back before native work.')
    blocked = {os.path.normcase(str(Path(item).resolve(strict=True)))
               for item in value['blocked_programs']}
    required = {os.path.normcase(str(Path(item).resolve(strict=True))) for item in executables}
    if not required <= blocked:
        raise AssertionError('A selected converter executable has no active external-address block.')
    boundary['selected_native_executables'] = sorted(required)
    boundary['policy_helper_sha256'] = sha256(script.read_bytes()).hexdigest()
    return boundary


def native_programs(calibre, office, pandoc):
    """The caller must block all executable descendants, not only python.exe."""
    result = {Path(sys.executable).resolve(), Path(sys._base_executable).resolve()}
    for selected in (Path(sys.executable).with_name('pythonw.exe'),
                     Path(sys._base_executable).with_name('pythonw.exe')):
        if selected.is_file():
            result.add(selected.resolve(strict=True))
    roots = (Path(calibre).parent, Path(office).parent.parent, Path(pandoc).parent)
    for root in roots:
        count = 0
        for parent, dirs, files in os.walk(root, followlinks=False):
            for name in (*dirs, *files):
                count += 1
                if count > 50000:
                    raise AssertionError('Selected native runtime inventory is unexpectedly large.')
                path = Path(parent) / name
                if path.is_symlink():
                    raise AssertionError('The qualification runtime contains a symbolic link.')
            for name in files:
                path = Path(parent) / name
                if path.suffix.casefold() in ('.exe', '.com'):
                    result.add(path.resolve(strict=True))
    result.update(Path(item).resolve(strict=True) for item in (calibre, office, pandoc))
    return tuple(sorted(result, key=str))


def call_native(args, stage, broker, *, selection=None, environment=None, timeout=120):
    from carr_library.audio.models import AudioBudget, AudioCleanupPending
    from carr_library.audio.process import run_process
    from carr_library.operations.models import ResourceRequest
    stage = Path(stage)
    request = ResourceRequest(cpu_slots=2, memory_bytes=3 * 1024**3,
                              disk_bytes=256 * 1024**2, disk_path=stage)
    snapshot = selection.snapshot() if selection is not None else None
    held = selection.lease(snapshot) if selection is not None else nullcontext()
    with broker.reserve(request, operation_id='fixture-' + sha256(str(stage).encode()).hexdigest()[:24],
                        timeout=30, run_timeout=timeout + 10) as lease:
        with held:
            try:
                return run_process([str(value) for value in args], cwd=stage,
                    env=environment, owned_windows_job=True, watch=(stage,),
                    budget=AudioBudget(timeout=timeout, max_log_bytes=512 * 1024,
                        max_output_bytes=256 * 1024**2, max_staging_files=10000))
            except AudioCleanupPending as error:
                lease.settle_cleanup(error)
                raise


def flat_document(kind, text):
    # Reused PR99 real native export fixtures, not a product format writer.
    body = {'doc': '<office:text><text:p>' + escape(text) + '</text:p></office:text>',
            'ppt': '<office:presentation><draw:page draw:name="Slide1"><draw:frame>'
                   '<draw:text-box><text:p>' + escape(text) + '</text:p></draw:text-box>'
                   '</draw:frame></draw:page></office:presentation>',
            'xls': '<office:spreadsheet><table:table table:name="Sheet1"><table:table-row>'
                   '<table:table-cell office:value-type="string"><text:p>' + escape(text) + '</text:p>'
                   '</table:table-cell></table:table-row></table:table></office:spreadsheet>'}[kind]
    mime = {'doc': 'text', 'ppt': 'presentation', 'xls': 'spreadsheet'}[kind]
    return ('''<?xml version="1.0" encoding="UTF-8"?>
<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
 office:version="1.3" office:mimetype="application/vnd.oasis.opendocument.''' + mime + '''">
<office:body>''' + body + '</office:body></office:document>').encode('utf-8')


def palmdoc(text):
    body = text.encode('ascii')
    header = bytearray(78)
    title = b'Synthetic PalmDoc\0'
    header[:len(title)] = title
    header[60:68] = b'TEXtREAd'
    header[76:78] = struct.pack('>H', 2)
    records = struct.pack('>II', 96, 0) + struct.pack('>II', 112, 1) + b'\0\0'
    metadata = struct.pack('>HHIHHI', 1, 0, len(body), 1, 4096, 0)
    return bytes(header) + records + metadata + body


def wordperfect(text):
    return b'\xffWPC' + struct.pack('<I', 32) + bytes((1, 10, 0, 0)) + b'\0' * 20 + text.encode('ascii')


def prepare(stage, library, calibre, office, pandoc, downloads):
    from carr_library.conversion.calibre import CalibreSelection, _environment
    from carr_library.conversion.office import OfficeSelection, PROFILE
    from carr_library.operations import ResourceBroker
    stage = Path(stage)
    stage.mkdir()
    broker = ResourceBroker.for_app(library.root)
    calibre = Path(calibre).resolve(strict=True)
    office = Path(office).resolve(strict=True)
    pandoc = Path(pandoc).resolve(strict=True)
    selected_calibre, selected_office = CalibreSelection.capture(calibre), OfficeSelection.capture(office)
    env = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR') if key in os.environ}
    env.update(PATH=os.pathsep.join((str(office.parent), str(Path(env.get('SYSTEMROOT', 'C:/Windows')) / 'System32'))),
               SAL_USE_VCLPLUGIN='svp', SAL_DISABLE_OPENCL='1', MAX_CONCURRENCY='1')
    versions = {}
    for name, executable, selection in (('calibre', calibre, selected_calibre),
                                        ('libreoffice', office, selected_office), ('pandoc', pandoc, None)):
        version_stage = stage / ('version-' + name)
        version_stage.mkdir()
        version_args = [executable, '--version']
        version_env = dict(env, TMPDIR=str(version_stage), TEMP=str(version_stage), TMP=str(version_stage))
        if name == 'libreoffice':
            profile = version_stage / 'profile'
            (profile / 'user').mkdir(parents=True)
            (profile / 'user/registrymodifications.xcu').write_bytes(PROFILE)
            version_args.insert(1, '-env:UserInstallation=' + profile.as_uri())
        elif name == 'calibre':
            for child in ('home', 'config', 'cache', 'temporary'):
                (version_stage / child).mkdir()
            version_env = _environment(version_stage, calibre.parent)
        result = call_native(version_args, version_stage, broker,
            selection=selection, environment=version_env, timeout=30)
        value = (result.stdout + result.stderr).decode('utf-8', 'replace').strip()
        expected = {'calibre': r'\b9\.19(?:\.0)?\b', 'libreoffice': r'\b26\.8\.3\.1\b',
                    'pandoc': r'\bpandoc 3\.14(?:\s|$)'}[name]
        if not re.search(expected, value) or len(value) > 16384:
            raise AssertionError('Actual selected native version differs: ' + name)
        versions[name] = value
    rows = []
    for kind in ('doc', 'ppt', 'xls'):
        directory = stage / ('office-' + kind)
        directory.mkdir()
        profile = directory / 'profile'
        (profile / 'user').mkdir(parents=True)
        (profile / 'user/registrymodifications.xcu').write_bytes(PROFILE)
        suffix = {'doc': 'fodt', 'ppt': 'fodp', 'xls': 'fods'}[kind]
        original = directory / ('synthetic.' + suffix)
        original.write_bytes(flat_document(kind, TEXT))
        filter_name = {'doc': 'MS Word 97', 'ppt': 'MS PowerPoint 97', 'xls': 'MS Excel 97'}[kind]
        call_native((office, '-env:UserInstallation=' + profile.as_uri(), '--headless', '--nologo',
                     '--norestore', '--convert-to', kind + ':' + filter_name,
                     '--outdir', str(directory), str(original)), directory, broker,
                    selection=selected_office, environment=env)
        generated = directory / ('synthetic.' + kind)
        if not generated.read_bytes().startswith(bytes.fromhex('d0cf11e0a1b11ae1')):
            raise AssertionError('The actual LibreOffice export did not produce the selected native container.')
        rows.append((generated, (TEXT,), 'synthetic native LibreOffice export'))
    for suffix, value in (('.wpd', wordperfect(TEXT)), ('.pdb', palmdoc(TEXT)), ('.prc', palmdoc(TEXT))):
        source = stage / ('synthetic' + suffix)
        source.write_bytes(value)
        rows.append((source, (TEXT,), 'synthetic native format bytes'))
    for kind in ('mobi', 'azw3', 'lit', 'tcr'):
        directory = stage / ('calibre-' + kind)
        directory.mkdir()
        for child in ('home', 'config', 'cache', 'temporary'):
            (directory / child).mkdir()
        original = directory / 'synthetic.html'
        original.write_text('<html><head><title>Synthetic reading document</title></head>'
            '<body><h1>Synthetic reading document</h1><p>' + TEXT + '</p></body></html>', encoding='utf-8')
        output = directory / ('synthetic.' + kind)
        args = [calibre, original, output, '--input-encoding', 'utf-8']
        if kind == 'mobi':
            args += ['--mobi-file-type', 'old']
        call_native(args, directory, broker, selection=selected_calibre,
                    environment=_environment(directory, calibre.parent))
        if not output.is_file() or not 0 < output.stat().st_size <= 64 * 1024**2:
            raise AssertionError('The real Calibre export is missing or oversized.')
        rows.append((output, (TEXT,), 'synthetic native Calibre export'))
        if kind == 'mobi':
            azw = directory / 'synthetic.azw'
            shutil.copyfile(output, azw)
            rows.append((azw, (TEXT,), 'same actual unprotected MOBI container under its AZW family suffix'))
    for name, suffix, needles in (('putty.chm', '.chm', ('PuTTY', 'SSH')),
                                 ('djvulibre-book-en.djvu', '.djvu', ('DjVu',))):
        target = stage / ('public' + suffix)
        shutil.copyfile(Path(downloads) / name, target)
        rows.append((target, needles, 'pinned public upstream document; not synthetic'))
    rtf = stage / 'synthetic.rtf'
    rtf.write_bytes(('{\\rtf1\\ansi ' + TEXT + '\\par Second paragraph.}').encode('ascii'))
    rows.append((rtf, (TEXT, 'Second paragraph.'), 'synthetic RTF source'))
    if {path.suffix for path, _, _ in rows} != {
            '.azw', '.azw3', '.chm', '.djvu', '.doc', '.lit', '.mobi', '.pdb',
            '.prc', '.ppt', '.tcr', '.wpd', '.xls', '.rtf'} or len(rows) != 14:
        raise AssertionError('Every required selected format needs one actual native input.')
    if broker.snapshot()['leases']:
        raise AssertionError('Fixture generation left native admission active.')
    return tuple(rows), versions


# Diagnostic only: observe the exact generated intermediate of the fixed,
# synthetic XLS case before its existing operation-owned staging is removed.
# No parser policy or successful-conversion contract is changed.
def failed_xls_structure(work, expected_source_sha256, *, check=lambda: None):
    from io import BytesIO
    from zipfile import ZipFile, ZIP_STORED, ZIP_DEFLATED
    from defusedxml import ElementTree
    from carr_library.conversion._selected_files import (
        checked_directory, regular_file, file_identity, verified_reader)
    if not callable(check) or not re.fullmatch(r'[0-9a-f]{64}', expected_source_sha256):
        raise ValueError('Invalid structural diagnostic authority.')
    def checkpoint():
        if check():
            raise InterruptedError('Structural diagnostic cancelled.')
    checkpoint()
    work = checked_directory(work)
    output = checked_directory(work / 'office-output')
    roots = [(path, (path.stat().st_dev, path.stat().st_ino)) for path in (work, output)]
    source, generated = work / 'source.xls', output / 'source.ods'
    source_info, output_info = regular_file(source), regular_file(generated)
    for info in (source_info, output_info):
        if info.st_nlink != 1 or not 0 < info.st_size <= 8 * 1024**2:
            raise ValueError('Structural diagnostic input exceeds its finite bound.')
    with (verified_reader(source, expected_identity=file_identity(source_info)) as original,
          verified_reader(generated, expected_identity=file_identity(output_info)) as intermediate):
        source_bytes = original.read(source_info.st_size + 1)
        checkpoint()
        data = intermediate.read(output_info.st_size + 1)
        if (len(source_bytes) != source_info.st_size
                or sha256(source_bytes).hexdigest() != expected_source_sha256
                or len(data) != output_info.st_size):
            raise ValueError('Structural diagnostic source identity changed.')
        checkpoint()
        with ZipFile(BytesIO(data)) as archive:
            members = archive.infolist()
            styles = [item for item in members if item.filename == 'styles.xml']
            if (len(members) > 4096 or len(styles) != 1
                    or sum(item.file_size for item in members) > 16 * 1024**2):
                raise ValueError('Structural diagnostic archive exceeds its finite inventory.')
            selected = styles[0]
            if (selected.flag_bits & 1 or selected.compress_type not in (ZIP_STORED, ZIP_DEFLATED)
                    or not 0 < selected.file_size <= 512 * 1024):
                raise ValueError('Structural diagnostic styles member is unsupported or oversized.')
            raw = archive.read(selected)
        checkpoint()
        root = ElementTree.fromstring(raw, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        namespaces = {
            'urn:oasis:names:tc:opendocument:xmlns:office:1.0': 'office',
            'urn:oasis:names:tc:opendocument:xmlns:style:1.0': 'style',
            'urn:oasis:names:tc:opendocument:xmlns:text:1.0': 'text',
            'urn:oasis:names:tc:opendocument:xmlns:table:1.0': 'table',
            'urn:oasis:names:tc:opendocument:xmlns:drawing:1.0': 'draw',
            'urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0': 'fo',
        }
        known = {
            'document-styles', 'styles', 'automatic-styles', 'master-styles', 'master-page',
            'header', 'header-first', 'header-left', 'footer', 'footer-first', 'footer-left',
            'region-left', 'region-center', 'region-right', 'p', 'span', 's', 'tab', 'line-break',
            'page-number', 'page-count', 'sheet-name', 'date', 'time', 'title', 'file-name',
            'display', 'visibility', 'style-name', 'name', 'page-layout-name', 'next-style-name',
            'text-properties', 'paragraph-properties', 'font-name', 'font-size', 'version',
        }
        def name(value):
            if not isinstance(value, str) or not value.startswith('{') or '}' not in value:
                return 'other'
            namespace, local = value[1:].split('}', 1)
            return namespaces[namespace] + ':' + local if namespace in namespaces and local in known else 'other'
        def shape(element, depth):
            if len(element.attrib) > 32:
                raise ValueError('Structural diagnostic attribute count exceeds its finite bound.')
            attributes = []
            for key, value in sorted(element.attrib.items()):
                label = name(key)
                attributes.append({'name': label, 'characters': len(value),
                    'non_whitespace': bool(value.strip()),
                    'fixed_value': value if label in ('style:display', 'text:display', 'fo:visibility')
                        and value in ('true', 'false', 'none', 'hidden', 'collapse', 'visible') else None})
            return {'tag': name(element.tag), 'depth': depth, 'attributes': attributes,
                'children': len(element), 'text_present': element.text is not None,
                'text_non_whitespace': bool((element.text or '').strip()),
                'tail_present': element.tail is not None,
                'tail_non_whitespace': bool((element.tail or '').strip())}
        office = '{urn:oasis:names:tc:opendocument:xmlns:office:1.0}'
        style = '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}'
        if root.tag != office + 'document-styles':
            raise ValueError('Structural diagnostic styles root differs.')
        targets = {style + item for item in (
            'header', 'header-first', 'header-left', 'footer', 'footer-first', 'footer-left')}
        summaries, visited, retained = [], 0, 0
        pending = [(root, ())]
        while pending:
            checkpoint()
            element, ancestors = pending.pop()
            visited += 1
            if visited > 20000 or len(ancestors) > 32:
                raise ValueError('Structural diagnostic XML exceeds its finite shape bound.')
            path = (*ancestors, name(element.tag))
            if element.tag in targets and element.get(style + 'display') == 'false':
                if len(summaries) >= 64:
                    raise ValueError('Structural diagnostic disabled-header count exceeds its finite bound.')
                descendants, queue = [], [(element, 0)]
                while queue:
                    checkpoint()
                    child, depth = queue.pop()
                    retained += 1
                    if retained > 256 or depth > 16:
                        raise ValueError('Structural diagnostic header descendants exceed their finite bound.')
                    descendants.append(shape(child, depth))
                    queue.extend((item, depth + 1) for item in reversed(list(child)))
                summaries.append({'path': list(path), 'nodes': descendants})
            pending.extend((item, path) for item in reversed(list(element)))
        checkpoint()
        for path, identity in roots:
            current = checked_directory(path).stat()
            if (current.st_dev, current.st_ino) != identity:
                raise ValueError('Structural diagnostic staging identity changed.')
        result = {'status': 'CAPTURED', 'scope': 'Exact failed synthetic XLS intermediate; structure only.',
            'source_sha256': expected_source_sha256,
            'intermediate_sha256': sha256(data).hexdigest(), 'intermediate_bytes': len(data),
            'styles_sha256': sha256(raw).hexdigest(), 'styles_bytes': len(raw),
            'xml_nodes': visited, 'disabled_headers': summaries,
            'document_text_retained': False, 'input_or_intermediate_exported': False,
            'format_acceptance': 'UNCHANGED', 'native_quality': 'NOT_RUN'}
        if len(json.dumps(result, ensure_ascii=True).encode('ascii')) > 128 * 1024:
            raise ValueError('Structural diagnostic report exceeds its finite byte bound.')
        return result


@contextmanager
def observe_failed_xls(output, expected_source_sha256, report, children):
    from carr_library.audio.models import AudioError
    from carr_library.conversion import selected
    from carr_library.conversion._selected_files import checked_directory
    output = checked_directory(output)
    info = output.stat()
    identity = info.st_dev, info.st_ino
    original = selected.run_process
    def observed(*arguments, **options):
        first = len(children)
        try:
            return original(*arguments, **options)
        except Exception as error:
            # Only a fully settled ordinary worker error admits read-only
            # diagnostics. Pending cleanup and cancellation keep their own path.
            try:
                owned = children[first:]
                if (type(error) is not AudioError or not owned
                        or any(child.poll() is None or child._job is not None for child in owned)):
                    report['failed_xls_structure'] = {'status': 'NOT_CAPTURED',
                        'reason': 'ordinary_error_and_quiescent_owner_required'}
                else:
                    current = checked_directory(output).stat()
                    work = checked_directory(options['cwd'])
                    if ((current.st_dev, current.st_ino) != identity
                            or work.name != 'work' or work.parent.parent != output
                            or not work.parent.name.startswith('.pending-conversion-')):
                        raise ValueError('Structural diagnostic work scope differs.')
                    report['failed_xls_structure'] = failed_xls_structure(work,
                        expected_source_sha256, check=options['cancelled'])
            except Exception as diagnostic_error:
                report['failed_xls_structure'] = {'status': 'NOT_CAPTURED',
                    'error_type': type(diagnostic_error).__name__}
            raise
    selected.run_process = observed
    try:
        yield
    finally:
        selected.run_process = original
