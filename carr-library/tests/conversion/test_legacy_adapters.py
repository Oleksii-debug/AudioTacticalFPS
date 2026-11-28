"""Boundaries for selected legacy adapters; synthetic tests do not certify vendors."""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import runpy
import struct
import subprocess
import sys
import time

import pytest

from carr_library.conversion import ConversionBudget, ConversionError, ConversionService, ConversionUnavailable
from carr_library.conversion.capabilities import capabilities
from carr_library.conversion import legacy, legacy_runtime
from carr_library.conversion.worker import convert_request
from carr_library.extract.page_ocr import Config

FIXTURES = runpy.run_path(str(Path(__file__).with_name('legacy_fixtures.py')))


def runtime(tmp_path, kind='chm'):
    root = tmp_path / ('runtime-' + kind)
    root.mkdir()
    names = {'chm': ('7z.exe', '7z.dll'), 'djvu': ('djvutxt.exe', 'djvused.exe', 'libdjvulibre-21.dll'),
             'calibre': ('ebook-convert.exe', 'calibre-debug.exe', 'python.zip')}[kind]
    for name in names:
        (root / name).write_bytes(('synthetic trusted test component ' + name).encode())
    return root / names[0]


def request(stage, suffix, raw, selected):
    source = stage / ('source' + suffix)
    source.write_bytes(raw)
    return dict(action='convert', source=str(source), output=str(stage / 'reading.txt'),
                report=str(stage / 'details.json'), sha256=sha256(raw).hexdigest(),
                source_size=len(raw), budget=asdict(ConversionBudget()),
                ocr_config=asdict(Config()), timeout=120.0,
                adapter={'kind': legacy.kind_for(suffix), 'runtime': selected})


@pytest.mark.parametrize('suffix', sorted(legacy.LEGACY_SUFFIXES))
def test_unexecuted_native_formats_never_become_available_from_selection_alone(suffix, monkeypatch):
    import importlib
    module = importlib.import_module('carr_library.conversion.capabilities')
    monkeypatch.setattr(module, 'QUALIFIED_SUFFIXES', frozenset())
    rows = {row['extension']: row for row in capabilities(
        calibre_selected=True, djvu_selected=True, chm_selected=True)}
    assert rows[suffix]['available'] is False
    assert rows[suffix]['qualification'] == 'native_fixture_not_qualified'


def test_qualified_djvu_requires_explicit_selection_and_does_not_enable_other_families():
    absent = {row['extension']: row for row in capabilities()}
    assert absent['.djvu']['available'] is False
    assert absent['.djvu']['qualification'] == 'explicit_selection_required'
    selected = {row['extension']: row for row in capabilities(djvu_selected=True)}
    assert selected['.djvu']['available'] is True
    assert selected['.djvu']['qualification'] == 'selected_runtime_requires_check'
    for suffix in legacy.LEGACY_SUFFIXES - {'.djvu'}:
        assert selected[suffix]['available'] is False
        assert selected[suffix]['qualification'] == 'explicit_selection_required'


@pytest.mark.parametrize('kind,path', [('calibre', 'ebook-convert.exe'), ('djvu', 'djvutxt.exe'), ('chm', '7z.exe')])
def test_relative_or_path_discovered_runtime_is_never_selected(kind, path, monkeypatch):
    monkeypatch.setenv('PATH', '/a/foreign/tool')
    with pytest.raises(ConversionUnavailable, match='absolute'):
        legacy_runtime.ToolSelection.capture(Path(path), kind)


@pytest.mark.parametrize('change', ['edit', 'replace', 'add', 'remove'])
def test_selected_component_change_cannot_start_a_conversion(tmp_path, change):
    executable = runtime(tmp_path)
    selection = legacy_runtime.ToolSelection.capture(executable, 'chm')
    component = executable.with_name('7z.dll')
    if change == 'edit':
        component.write_bytes(b'changed bytes')
    elif change == 'replace':
        component.unlink()
        component.write_bytes(b'replaced bytes')
    elif change == 'add':
        executable.with_name('plugin.dll').write_bytes(b'foreign')
    else:
        component.unlink()
    stage = tmp_path / 'stage'
    stage.mkdir()
    with pytest.raises(ConversionError, match='changed'):
        selection.snapshot(stage)
    assert not list(stage.iterdir())


def test_large_component_inventory_keeps_existing_worker_request_bound(tmp_path):
    executable = runtime(tmp_path)
    for index in range(700):
        executable.with_name('data-' + str(index) + '.txt').write_text(str(index))
    stage = tmp_path / 'stage'
    stage.mkdir()
    descriptor = legacy_runtime.ToolSelection.capture(executable, 'chm').snapshot(stage)
    assert len(json.dumps(descriptor).encode()) < 1000
    assert descriptor['bytes'] > 64_000
    value = legacy_runtime._load(descriptor, stage, 'chm', time.monotonic() + 30)
    assert len(value['files']) == 702


def test_cancel_during_runtime_snapshot_creates_no_manifest(tmp_path):
    executable = runtime(tmp_path)
    selected = legacy_runtime.ToolSelection.capture(executable, 'chm')
    stage = tmp_path / 'stage'
    stage.mkdir()
    with pytest.raises(InterruptedError):
        selected.snapshot(stage, cancelled=lambda: True)
    assert not list(stage.iterdir())


def test_resealed_foreign_inventory_path_is_not_authority(tmp_path):
    executable = runtime(tmp_path)
    stage = tmp_path / 'stage'
    stage.mkdir()
    selected = legacy_runtime.ToolSelection.capture(executable, 'chm').snapshot(stage)
    external = tmp_path / 'foreign.json'
    external.write_bytes((stage / legacy_runtime.INVENTORY_NAME).read_bytes())
    selected['path'] = str(external)
    with pytest.raises(ConversionError, match='private'):
        legacy_runtime._load(selected, stage, 'chm', time.monotonic() + 30)


def test_unqualified_service_requires_explicit_fixture_preview(tmp_path, monkeypatch):
    source = tmp_path / 'source.chm'
    source.write_bytes(FIXTURES['sample_chm']())
    output = tmp_path / 'output'
    output.mkdir()
    monkeypatch.setattr('carr_library.conversion.service.QUALIFIED_SUFFIXES', frozenset())
    service = ConversionService(output, input_roots=(source,), chm=runtime(tmp_path))
    monkeypatch.setattr('carr_library.conversion.service._invoke', lambda *a, **k: pytest.fail('worker must not start'))
    with pytest.raises(ConversionUnavailable, match='qualification'):
        service.extract(source, 'reading.txt')
    assert not list(output.iterdir())


@pytest.mark.parametrize('suffix', ['.mobi', '.prc'])
def test_encrypted_palm_header_is_refused_before_native_reader(suffix):
    with pytest.raises(ConversionUnavailable, match='Encrypted'):
        legacy._calibre_header(FIXTURES['palm'](encrypted=True), suffix)


@pytest.mark.parametrize('marker', [b'/DRMStorage/Licenses/EUL', b'/DRMStorage/DRMBookplate'])
def test_protected_lit_marker_is_refused_before_calibre(marker):
    raw = b'ITOLITLS' + struct.pack('<I', 1) + b'\0' * 40 + marker
    with pytest.raises(ConversionUnavailable, match='Protected'):
        legacy._calibre_header(raw, '.lit')


@pytest.mark.parametrize('suffix,raw', [
    ('.mobi', b'\xeaDRMION\xee' + b'\0' * 200), ('.azw', b'TPZ0' + b'\0' * 200),
    ('.azw3', b'not a book'), ('.lit', b'ordinary renamed text'), ('.tcr', b'ordinary renamed text'),
])
def test_renamed_or_foreign_book_formats_are_not_conversion_inputs(suffix, raw):
    with pytest.raises(ConversionError):
        legacy._calibre_header(raw, suffix)


def test_djvu_shared_or_external_reference_is_refused_before_native_reader():
    raw = FIXTURES['iff'](b'DJVU', (b'INCL', b'outside.djvu'))
    with pytest.raises(ConversionUnavailable, match='references'):
        legacy._djvu_pages(raw, 10)


@pytest.mark.parametrize('mutation', ['truncated', 'size', 'page_budget', 'indirect'])
def test_djvu_framing_and_page_authority_are_bounded(mutation):
    raw = FIXTURES['iff'](b'DJVU', (b'INFO', b'\0' * 10))
    maximum = 10
    if mutation == 'truncated':
        raw = raw[:-1]
    elif mutation == 'size':
        raw = raw[:8] + struct.pack('>I', 0xffffffff) + raw[12:]
    elif mutation == 'page_budget':
        maximum = 0
    else:
        raw = FIXTURES['iff'](b'DJVM', (b'DIRM', b'\0\0\0'))
    with pytest.raises(ConversionError):
        legacy._djvu_pages(raw, maximum)


@pytest.mark.parametrize('nested', [False, True])
@pytest.mark.parametrize('padded', [False, True])
def test_djvu_final_odd_chunk_ends_exactly_at_its_containing_form(nested, padded):
    page = b'DJVU' + b'INFO' + struct.pack('>I', 3) + b'abc' + (b'\0' if padded else b'')
    if nested:
        # DIRM is aligned before FORM. The final FORM, like its INFO child,
        # is permitted to end at the parent's declared odd byte boundary.
        payload = (b'DJVM' + b'DIRM' + struct.pack('>I', 1) + b'\x80\0' +
                   b'FORM' + struct.pack('>I', len(page)) + page)
    else:
        payload = page
    raw = b'AT&TFORM' + struct.pack('>I', len(payload)) + payload
    assert legacy._djvu_pages(raw, 1) == 1


@pytest.mark.parametrize('malformation', ['overrun', 'unaligned_sibling'])
def test_djvu_final_padding_exception_never_relaxes_chunk_bounds_or_sibling_alignment(malformation):
    if malformation == 'overrun':
        payload = b'DJVU' + b'INFO' + struct.pack('>I', 4) + b'abc'
    else:
        payload = (b'DJVU' + b'INFO' + struct.pack('>I', 3) + b'abc' +
                   b'JUNK' + struct.pack('>I', 4) + b'data')
    raw = b'AT&TFORM' + struct.pack('>I', len(payload)) + payload
    with pytest.raises(ConversionError, match='chunk'):
        legacy._djvu_pages(raw, 1)


@pytest.mark.parametrize('name', ['../private.htm', 'C:/private.htm', '/absolute.htm', 'a//b.htm', 'a/./b.htm', 'a.htm\nSize = 0'])
def test_chm_member_path_cannot_escape_or_spoof_listing(name):
    with pytest.raises(ConversionError):
        legacy._member(name)


@pytest.mark.parametrize('raw', [
    b'Path = a.htm\nSize = 3\n\nPath = A.HTM\nSize = 4',
    b'Path = a.htm\nSize = 3\nSize = 4',
    b'Path = a.htm\nSize = 3\nEncrypted = +',
    b'Path = a.htm\nSize = 10000',
    b'Path = a.htm\nSize = 3\nUnknown = value',
])
def test_chm_ambiguous_or_excessive_listing_never_selects_members(raw):
    with pytest.raises(ConversionError):
        legacy._chm_listing(raw, 100)


def test_chm_worker_uses_declared_toc_and_existing_inert_html_reader(tmp_path, monkeypatch):
    executable = runtime(tmp_path)
    stage = tmp_path / 'stage'
    stage.mkdir()
    selected = legacy_runtime.ToolSelection.capture(executable, 'chm').snapshot(stage)
    members = {
        'a.htm': (b'<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">'
                  b'<html><body><p>Second page<div></div></p>'
                  b'<script>UNSAFE_SCRIPT_TEXT</script></body></html>'),
        'z.htm': b'<html><body><p>First page</p></body></html>',
        'contents.hhc': b'<param name="Local" value="z.htm"><param name="Local" value="a.htm">',
    }
    calls = []
    def native(args, *other, **options):
        calls.append(args)
        if args[1] == 'l':
            return '\n\n'.join('Path = ' + name + '\nSize = ' + str(len(raw)) + '\nMethod = Copy'
                               for name, raw in members.items()).encode()
        assert args[1:7] == ['x', '-so', '-y', '-spd', '-sccUTF-8', '-tchm']
        return members[args[-1]]
    monkeypatch.setattr(legacy, '_run', native)
    result = convert_request(request(stage, '.chm', FIXTURES['sample_chm'](), selected))
    text = (stage / 'reading.txt').read_text()
    assert result['state'] == 'complete' and text == 'First page\n\nSecond page'
    assert 'UNSAFE_SCRIPT_TEXT' not in text
    details = json.loads((stage / 'details.json').read_text())
    assert [row['logical_locator']['chm_member'] for row in details['segments']] == ['z.htm', 'a.htm']
    assert all(row['physical_index'] is None and row['status'] == 'review_required' for row in details['segments'])
    assert all('--' in arguments for arguments in calls)
    for row in details['segments']:
        locator = row['logical_locator']
        original_member = members[locator['chm_member']]
        assert locator['member_sha256'] == sha256(original_member).hexdigest()
        assert locator['parsed_member_sha256'] == sha256(legacy._chm_html(original_member)).hexdigest()


def test_chm_truncated_member_publishes_no_complete_worker_report(tmp_path, monkeypatch):
    executable = runtime(tmp_path)
    stage = tmp_path / 'stage'
    stage.mkdir()
    selected = legacy_runtime.ToolSelection.capture(executable, 'chm').snapshot(stage)
    monkeypatch.setattr(legacy, '_run', lambda args, *a, **k:
                        b'Path = a.htm\nSize = 10' if args[1] == 'l' else b'bad')
    with pytest.raises(ConversionError, match='differ'):
        convert_request(request(stage, '.chm', FIXTURES['sample_chm'](), selected))
    assert not (stage / 'reading.txt').exists() and not (stage / 'details.json').exists()


@pytest.mark.parametrize('operation', [
    'outside_read', 'outside_write', 'socket', 'process', 'allowed_output',
    'cleanup_file', 'cleanup_directory', 'cleanup_outside_file', 'cleanup_outside_directory',
    'cleanup_program', 'cleanup_unknown', 'version_previous', 'version_newer',
])
def test_fixed_calibre_wrapper_enforces_private_io_and_no_network_or_programs(tmp_path, operation):
    stage, root = tmp_path / 'stage', tmp_path / 'runtime'
    stage.mkdir()
    root.mkdir()
    (stage / 'calibre-config').mkdir()
    source, target = stage / 'source.tcr', stage / 'calibre.txt'
    source.write_bytes(b'!!8-Bit!!synthetic')
    target.write_bytes(b'')
    helper = stage / 'calibre_guard.py'
    helper.write_bytes(Path(legacy.__file__).with_name('calibre_guard.py').read_bytes())
    sentinel = tmp_path / 'private.txt'
    sentinel.write_text('PRIVATE_MARKER', encoding='utf-8')
    program = r'''
import os, pathlib, runpy, socket, subprocess, sys, types
for name in ('calibre', 'calibre.ebooks', 'calibre.ebooks.conversion', 'calibre.ebooks.conversion.cli',
             'calibre.constants', 'calibre.utils', 'calibre.utils.safe_atexit'):
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
sys.modules['calibre.constants'].__version__ = '9.19.0'
cleanup = sys.modules['calibre.utils.safe_atexit']
cleanup.worker = None
cleanup.RMTREE_ACTION, cleanup.UNLINK_ACTION, cleanup.RUN_PROGRAM_ACTION = 'rmtree', 'unlink', 'run_program'
def unexpected_cleanup_worker(*args):
    raise AssertionError('The generic vendor cleanup worker must not start.')
cleanup._send_command = unexpected_cleanup_worker
operation, private = sys.argv[1:3]
if operation in ('version_previous', 'version_newer'):
    sys.modules['calibre.constants'].__version__ = (
        '9.18.0' if operation == 'version_previous' else '9.19.1')
def convert(args):
    if operation == 'outside_read':
        pathlib.Path(private).read_text()
    elif operation == 'outside_write':
        pathlib.Path(private).write_text('CHANGED')
    elif operation == 'socket':
        socket.socket()
    elif operation == 'process':
        subprocess.run([sys.executable, '-c', 'pass'], check=True)
    elif operation.startswith('cleanup_'):
        local = pathlib.Path(args[2]).parent / 'calibre-temp'
        local.mkdir()
        child = local / 'still-owned.tmp'
        child.write_text('RETAIN UNTIL NATIVE EXIT', encoding='utf-8')
        if operation == 'cleanup_file':
            cleanup._send_command('unlink', str(child))
        elif operation == 'cleanup_directory':
            cleanup._send_command('rmtree', str(local))
        elif operation == 'cleanup_outside_file':
            cleanup._send_command('unlink', private)
        elif operation == 'cleanup_outside_directory':
            cleanup._send_command('rmtree', str(pathlib.Path(private).parent))
        elif operation == 'cleanup_program':
            cleanup._send_command('run_program', [sys.executable, '-c', 'pass'])
        else:
            cleanup._send_command('unreviewed', str(child))
        assert child.read_text(encoding='utf-8') == 'RETAIN UNTIL NATIVE EXIT'
    pathlib.Path(args[2]).write_text('Allowed output', encoding='utf-8')
    return 0
sys.modules['calibre.ebooks.conversion.cli'].main = convert
sys.argv = sys.argv[3:]
os.environ['CALIBRE_CONFIG_DIRECTORY'] = str(pathlib.Path(sys.argv[0]).parent / 'calibre-config')
runpy.run_path(sys.argv[0], run_name='__main__')
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', program, operation, str(sentinel),
        str(helper), str(source), str(target), str(root), '1024'],
        cwd=stage, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False)
    if operation in ('allowed_output', 'cleanup_file', 'cleanup_directory'):
        assert result.returncode == 0, result.stderr.decode('utf-8', 'replace')
        assert target.read_text() == 'Allowed output'
        if operation.startswith('cleanup_'):
            assert (stage / 'calibre-temp' / 'still-owned.tmp').read_text() == 'RETAIN UNTIL NATIVE EXIT'
    else:
        assert result.returncode != 0
        assert target.read_bytes() == b''
    if operation in ('version_previous', 'version_newer'):
        assert 'not been qualified for the selected calibre version' in result.stderr.decode('utf-8', 'replace')
    assert sentinel.read_text() == 'PRIVATE_MARKER'


@pytest.mark.parametrize('raw', [b'!!8-Bit!!', b'!!8-Bit!!' + b'\xff' + b'x' * 5,
                                b'!!8-Bit!!' + b'\xff' + b'x' * 255 + b'\0' * 255 + b'\0' * 20])
def test_tcr_dictionary_and_actual_expansion_are_bounded_before_native_decoder(raw):
    with pytest.raises(ConversionError, match='dictionary|budget'):
        legacy._calibre_header(raw, '.tcr', 1024)


def test_windows_cached_directory_metadata_does_not_invalidate_regular_components(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from carr_library.audio.jobs import file_identity
    executable = runtime(tmp_path)
    scandir = legacy_runtime.os.scandir
    class Entry:
        def __init__(self, original):
            self.path, self.name = original.path, original.name
            self.original = original
        def stat(self, *, follow_symlinks=True):
            value = self.original.stat(follow_symlinks=follow_symlinks)
            return SimpleNamespace(st_mode=value.st_mode, st_size=value.st_size,
                st_mtime_ns=value.st_mtime_ns, st_ctime_ns=value.st_ctime_ns,
                st_file_attributes=getattr(value, 'st_file_attributes', 0),
                st_nlink=0, st_ino=0, st_dev=0)
    class Scan:
        def __init__(self, path):
            self.stream = scandir(path)
        def __enter__(self):
            return iter(Entry(entry) for entry in self.stream)
        def __exit__(self, *args):
            self.stream.close()
    monkeypatch.setattr(legacy_runtime.os, 'scandir', Scan)
    selected = legacy_runtime.ToolSelection.capture(executable, 'chm')
    assert dict(selected.files) == {
        path.name: file_identity(path.lstat()) for path in (executable, executable.with_name('7z.dll'))}
    stage = tmp_path / 'stage'
    stage.mkdir()
    descriptor = selected.snapshot(stage)
    value = legacy_runtime._load(descriptor, stage, 'chm', time.monotonic() + 30)
    assert {row['name'] for row in value['files']} == {'7z.exe', '7z.dll'}


def test_real_hardlink_alias_remains_refused_after_fresh_windows_stat(tmp_path):
    executable = runtime(tmp_path)
    alias = tmp_path / 'foreign-hardlink.dll'
    legacy_runtime.os.link(executable.with_name('7z.dll'), alias)
    assert alias.stat().st_nlink == 2
    with pytest.raises(ConversionUnavailable, match='unsupported'):
        legacy_runtime.ToolSelection.capture(executable, 'chm')


@pytest.mark.parametrize('last', ['Block', 'Method'])
def test_native_chm_empty_final_property_keeps_its_delimiter(last):
    raw = ('Path = page.htm\r\nSize = 12\r\n' + last + ' = \r\n\r\n').encode()
    assert legacy._chm_listing(raw, 100) == [{'name': 'page.htm', 'bytes': 12, 'directory': False}]


@pytest.mark.parametrize('public,system', [
    ('-//W3C//DTD HTML 3.2 Final//EN', ''),
    ('-//W3C//DTD HTML 4.0 Transitional//EN', ''),
    ('-//W3C//DTD HTML 4.01 Transitional//EN', 'http://www.w3.org/TR/html4/loose.dtd'),
    ('-//W3C//DTD HTML 4.01//EN', 'http://www.w3.org/TR/html4/strict.dtd'),
])
def test_chm_known_legacy_doctype_is_inert_and_original_bytes_stay_bound(public, system):
    from carr_library.extract.document_formats import extract_document
    body = b'<html><body><p>Local reading text.</p><script>ACTIVE_SECRET</script></body></html>'
    declaration = ('<!DOCTYPE HTML PUBLIC "' + public + '"' + (' "' + system + '"' if system else '') + '>').encode()
    original = declaration + body
    reading = legacy._chm_html(original)
    assert reading == b'<!DOCTYPE html>' + body and original == declaration + body
    extracted = extract_document(reading, '.html')
    text = '\n'.join(row['raw_text'] for row in extracted['records'])
    assert 'Local reading text.' in text and 'ACTIVE_SECRET' not in text


@pytest.mark.parametrize('declaration', [
    b'<!DOCTYPE HTML SYSTEM "file:///private.txt">',
    b'<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0 Transitional//EN" "https://unselected.example/dtd">',
    b'<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0 Transitional//EN" [<!ENTITY private SYSTEM "file:///private.txt">]>',
    b'<!DOCTYPE HTML PUBLIC "-//UNSELECTED//DTD HTML 4.0//EN">',
])
def test_chm_undeclared_or_complex_doctype_remains_refused(declaration):
    from carr_library.extract.document_formats import extract_document
    raw = declaration + b'<html><body><p>Reading text.</p></body></html>'
    assert legacy._chm_html(raw) == raw
    with pytest.raises(ValueError, match='document type|doctype|DOCTYPE'):
        extract_document(raw, '.html')


@pytest.mark.parametrize('outcome', ['complete', 'native-error'])
def test_large_selected_code_closure_keeps_every_reader_until_native_scope_exits(
        tmp_path, monkeypatch, outcome):
    from contextlib import contextmanager
    executable = runtime(tmp_path, 'calibre')
    for index in range(382):
        (executable.parent / ('component-' + str(index) + '.dll')).write_bytes(b'SYNTHETIC CODE IDENTITY')
    stage = tmp_path / 'stage'
    stage.mkdir()
    selected = legacy_runtime.ToolSelection.capture(executable, 'calibre').snapshot(stage)
    real, streams = legacy_runtime.verified_reader, []

    @contextmanager
    def tracked(path, **options):
        with real(path, **options) as stream:
            streams.append(stream)
            yield stream
    monkeypatch.setattr(legacy_runtime, 'verified_reader', tracked)
    try:
        with legacy_runtime.admitted_runtime(selected, stage, 'calibre', time.monotonic() + 60):
            assert sum(not stream.closed for stream in streams) == 385
            if outcome == 'native-error':
                raise RuntimeError('synthetic native failure')
    except RuntimeError as error:
        assert outcome == 'native-error' and str(error) == 'synthetic native failure'
    assert streams and all(stream.closed for stream in streams)
    for path in executable.parent.iterdir():
        with path.open('r+b') as stream:
            stream.write(b'RELEASED')


def test_excessive_code_handle_inventory_is_refused_before_any_native_lease(tmp_path, monkeypatch):
    value = {'root': str(tmp_path), 'executable': 'ebook-convert.exe',
             'files': [{'name': 'component-' + str(index) + '.dll', 'identity': [0] * 6}
                       for index in range(legacy_runtime.MAX_CODE_HANDLES + 1)]}
    monkeypatch.setattr(legacy_runtime, '_load', lambda *a, **k: value)
    monkeypatch.setattr(legacy_runtime, 'verified_reader',
                        lambda *a, **k: pytest.fail('Excessive native inventory opened a code lease.'))
    with pytest.raises(ConversionUnavailable, match='handle budget'):
        with legacy_runtime.admitted_runtime({'sha256': '0' * 64}, tmp_path, 'calibre',
                                            time.monotonic() + 5):
            pytest.fail('Excessive native inventory reached execution.')


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-8-sig', 'cp1251'])
def test_chm_legacy_orphan_paragraph_end_preserves_decoding_and_text_order(encoding):
    from carr_library.extract.document_formats import extract_document
    declaration = '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">'
    charset = 'utf-8' if encoding == 'utf-8-sig' else encoding
    text = (declaration + '<html><head><meta charset="' + charset + '"></head><body>'
            '<p>До<table><tr><td>Середина</td></tr></table>\r\n</P>\n<p>Після</p></body></html>')
    original = text.encode(encoding)
    notices = []
    reading = legacy._chm_html(original, notices=notices)
    assert original == text.encode(encoding)
    assert '<p></P>' in reading.decode(encoding)
    assert notices == ['legacy_html_orphan_paragraph_end_normalized']
    extracted = extract_document(reading, '.html')
    words = '\n'.join(row['raw_text'] for row in extracted['records'])
    assert words.index('До') < words.index('Середина') < words.index('Після')
    assert all(row['physical_index'] is None and row['printed_label'] is None
               for row in extracted['records'])


def test_chm_paragraph_normalization_keeps_active_hidden_and_literal_tokens_inert():
    from carr_library.extract.document_formats import extract_document
    raw = (b'<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">'
           b'<html><body>VISIBLE</p>'
           b'<script>const text = "</p>"; ACTIVE_SCRIPT</script>'
           b'<style>.x { content: "</p>"; } ACTIVE_STYLE</style>'
           b'<object><p>ACTIVE_OBJECT<div></div></p></object>'
           b'<div hidden>HIDDEN_VALUE</p></div><p>END</p></body></html>')
    reading = legacy._chm_html(raw)
    assert b'<script>const text = "</p>"; ACTIVE_SCRIPT</script>' in reading
    assert b'<style>.x { content: "</p>"; } ACTIVE_STYLE</style>' in reading
    extracted = extract_document(reading, '.html')
    text = '\n'.join(row['raw_text'] for row in extracted['records'])
    assert 'VISIBLE' in text and 'END' in text
    assert not any(value in text for value in ('ACTIVE_SCRIPT', 'ACTIVE_STYLE', 'ACTIVE_OBJECT', 'HIDDEN_VALUE'))


@pytest.mark.parametrize('body', [
    b'<html><body>text</div></body></html>',
    b'<html><body><p><b>text<table></table></b></p></body></html>',
    b'<html><body>text</p extra="unsupported"></body></html>',
])
def test_chm_paragraph_normalization_does_not_relax_other_malformed_structure(body):
    raw = b'<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN">' + body
    with pytest.raises(ValueError):
        legacy._chm_html(raw)


def test_chm_undeclared_html_and_general_html_reader_keep_their_strict_endtag_contract():
    from carr_library.extract.document_formats import extract_document
    raw = b'<!DOCTYPE html><html><body>Reading</p></body></html>'
    assert legacy._chm_html(raw) == raw
    with pytest.raises(ValueError, match='nesting'):
        extract_document(raw, '.html')


@pytest.mark.parametrize('limit', ['text', 'deadline'])
def test_chm_paragraph_normalization_uses_current_text_and_absolute_time_budgets(limit):
    raw = (b'<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN">'
           b'<html><body>Bounded reading text</p></body></html>')
    options = {'max_chars': 2} if limit == 'text' else {'deadline': time.monotonic() - 1}
    expected = ValueError if limit == 'text' else TimeoutError
    with pytest.raises(expected):
        legacy._chm_html(raw, **options)


@pytest.mark.parametrize('suffix', ['.azw', '.azw3', '.lit', '.mobi', '.prc', '.tcr'])
def test_qualified_calibre_formats_still_require_explicit_selection_and_do_not_select_chm(suffix):
    absent = {row['extension']: row for row in capabilities()}
    assert absent[suffix]['available'] is False
    assert absent[suffix]['qualification'] == 'explicit_selection_required'
    selected = {row['extension']: row for row in capabilities(calibre_selected=True)}
    assert selected[suffix]['available'] is True
    assert selected[suffix]['qualification'] == 'selected_runtime_requires_check'
    assert '9.19.0' in selected[suffix]['limitation']
    assert selected['.djvu']['available'] is False
    assert selected['.chm']['available'] is False
    assert selected['.chm']['qualification'] == 'explicit_selection_required'


def test_qualified_chm_requires_explicit_selection_and_does_not_select_other_families():
    absent = {row['extension']: row for row in capabilities()}
    assert absent['.chm']['available'] is False
    assert absent['.chm']['qualification'] == 'explicit_selection_required'
    selected = {row['extension']: row for row in capabilities(chm_selected=True)}
    assert selected['.chm']['available'] is True
    assert selected['.chm']['qualification'] == 'selected_runtime_requires_check'
    assert '26.06' in selected['.chm']['limitation']
    for suffix in legacy.LEGACY_SUFFIXES - {'.chm'}:
        assert selected[suffix]['available'] is False
        assert selected[suffix]['qualification'] == 'explicit_selection_required'
