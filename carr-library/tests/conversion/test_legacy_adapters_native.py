"""Required real optional-tool fixtures. No skip or PATH fallback is accepted."""
from hashlib import sha256
import os
from pathlib import Path
import runpy
import struct
import subprocess

import pytest

from carr_library.conversion import ConversionBudget, ConversionError, ConversionService
from carr_library.conversion.legacy import _djvu_pages

FIXTURES = runpy.run_path(str(Path(__file__).with_name('legacy_fixtures.py')))


def selected(kind):
    variable = {'calibre': 'CARR_CALIBRE', 'djvu': 'CARR_DJVUTXT', 'chm': 'CARR_7ZIP'}[kind]
    value = os.environ.get(variable)
    assert value, 'Required explicit optional converter was not provisioned: ' + variable
    path = Path(value)
    assert path.is_absolute() and path.is_file()
    return path


def native(args, stage, *, calibre=False):
    env = {key: value for key, value in os.environ.items() if key in ('SYSTEMROOT', 'WINDIR', 'LANG')}
    env['PATH'] = os.pathsep.join((str(Path(args[0]).parent),
                                  str(Path(env.get('SYSTEMROOT', 'C:/Windows')) / 'System32')))
    env.update(TEMP=str(stage), TMP=str(stage), TMPDIR=str(stage), PYTHONDONTWRITEBYTECODE='1')
    if calibre:
        config, temporary = stage / 'fixture-config', stage / 'fixture-temp'
        config.mkdir(exist_ok=True)
        temporary.mkdir(exist_ok=True)
        env.update(CALIBRE_CONFIG_DIRECTORY=str(config), CALIBRE_TEMP_DIR=str(temporary),
                   CALIBRE_CACHE_DIRECTORY=str(temporary), CALIBRE_ALLOW_PYTHON_TEMPLATES='0',
                   CALIBRE_NO_DEFAULT_PROGRAMS='1', QT_QPA_PLATFORM='offscreen')
    result = subprocess.run([str(value) for value in args], cwd=stage, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=90, check=False)
    assert result.returncode == 0, (result.stdout + result.stderr).decode('utf-8', 'replace')[-4000:]
    return result.stdout


def book(tmp_path, suffix, text='FIRST CALIBRE READING PAGE\n\nSECOND SOURCE PARAGRAPH.'):
    stage = tmp_path / ('fixture-' + suffix[1:])
    stage.mkdir()
    source = stage / 'synthetic.txt'
    source.write_text(text, encoding='utf-8')
    extension = '.mobi' if suffix in ('.azw', '.prc') else suffix
    binary = stage / ('synthetic' + extension)
    args = [selected('calibre'), source, binary, '--input-encoding=utf-8']
    if extension == '.mobi':
        args += ['--mobi-file-type=old']
    native(args, stage, calibre=True)
    assert binary.is_file() and binary.stat().st_size > 32
    if extension != suffix:
        # These are documented alternate extensions for the same actual MOBI
        # binary container, not text renamed to masquerade as a legacy book.
        alias = stage / ('synthetic' + suffix)
        alias.write_bytes(binary.read_bytes())
        binary = alias
    return binary


def djvu(tmp_path, *, multiple=False, text=True):
    stage = tmp_path / 'djvu-fixture'
    stage.mkdir()
    root = selected('djvu').parent
    pages = []
    for index in range(1, 3 if multiple else 2):
        pbm = stage / ('page-' + str(index) + '.pbm')
        pbm.write_bytes(b'P4\n64 64\n' + b'\0' * 512)
        output = stage / ('page-' + str(index) + '.djvu')
        native([root / 'cjb2.exe', pbm, output], stage)
        if text:
            text_file = stage / 'text.sexpr'
            text_file.write_text('(page 0 0 64 64 "DJVU READING PAGE ' + str(index) + '")\n', encoding='utf-8')
            script = stage / 'set-text.djvused'
            script.write_text('select 1\nset-txt text.sexpr\n', encoding='utf-8')
            native([root / 'djvused.exe', output, '-s', '-f', script], stage)
        assert output.read_bytes().startswith(b'AT&TFORM')
        pages.append(output)
    if multiple:
        output = stage / 'bundled.djvu'
        native([root / 'djvm.exe', '-c', output, *pages], stage)
        return output
    return pages[0]


def convert(tmp_path, source, kind, *, budget=None, cancelled=lambda: False):
    output = tmp_path / 'reading-output'
    output.mkdir()
    result = ConversionService(output, input_roots=(source,), budget=budget,
        legacy_preview=kind != 'djvu', **{kind: selected(kind)}).extract(source, 'reading.txt', cancelled=cancelled)
    return result


def prove(result, source, before):
    assert source.read_bytes() == before
    assert result.report['source_sha256'] == sha256(before).hexdigest()
    assert result.report['text_sha256'] == sha256(result.path.read_bytes()).hexdigest()
    assert result.report['original_modified'] is False
    assert result.report['verified_for_quotation'] is False
    assert result.report['printed_pages_reconstructed'] is False
    assert all(row['physical_index'] is None and row['printed_label'] is None
               and row['status'] == 'review_required' for row in result.report['segments'])
    assert len(list(result.path.parent.iterdir())) == 2


@pytest.mark.parametrize('suffix', ['.azw', '.azw3', '.lit', '.mobi', '.prc', '.tcr'])
def test_real_calibre_unprotected_format_retains_original_and_bound_text(tmp_path, suffix):
    source = book(tmp_path, suffix)
    before = source.read_bytes()
    try:
        result = convert(tmp_path, source, 'calibre')
    except ConversionError:
        if suffix == '.tcr':
            diagnose = runpy.run_path(str(Path(__file__).with_name('native_diagnostics.py')))['diagnose_selected_fixture']
            diagnose('calibre', source, selected('calibre'), tmp_path / 'reading-output')
        raise
    assert 'FIRST CALIBRE READING PAGE' in result.path.read_text(encoding='utf-8')
    assert 'SECOND SOURCE PARAGRAPH.' in result.path.read_text(encoding='utf-8')
    assert result.report['engine'] == 'selected-calibre-' + suffix[1:]
    prove(result, source, before)


def test_real_calibre_protected_palm_input_is_refused_without_decryption(tmp_path):
    source = tmp_path / 'protected.prc'
    source.write_bytes(FIXTURES['palm'](encrypted=True))
    before = source.read_bytes()
    with pytest.raises(ConversionError, match='Encrypted'):
        convert(tmp_path, source, 'calibre')
    assert source.read_bytes() == before and not list((tmp_path / 'reading-output').iterdir())


def test_real_calibre_output_budget_publishes_nothing(tmp_path):
    source = book(tmp_path, '.tcr')
    before = source.read_bytes()
    with pytest.raises(ConversionError, match='budget|limit'):
        convert(tmp_path, source, 'calibre', budget=ConversionBudget(max_text_bytes=4))
    assert source.read_bytes() == before and not list((tmp_path / 'reading-output').iterdir())


def test_real_calibre_cancel_after_native_text_settles_before_private_cleanup(tmp_path):
    source = tmp_path / 'cancellation.tcr'
    source.write_bytes(FIXTURES['tcr'](b'CALIBRE NATIVE CANCEL READING TEXT.\n\n' * 1000))
    before = source.read_bytes()
    observed = []
    def cancelled():
        paths = list((tmp_path / 'reading-output').glob('.carr-conversion-*/calibre.txt'))
        if any(path.stat().st_size > 0 for path in paths):
            observed.append(True)
        return bool(observed)
    with pytest.raises(InterruptedError):
        convert(tmp_path, source, 'calibre', cancelled=cancelled)
    assert observed
    assert source.read_bytes() == before and not list((tmp_path / 'reading-output').iterdir())


@pytest.mark.parametrize('multiple', [False, True], ids=['standalone-page', 'bundled-pages'])
def test_real_djvulibre_reads_every_embedded_text_page(tmp_path, multiple):
    source = djvu(tmp_path, multiple=multiple)
    before = source.read_bytes()
    result = convert(tmp_path, source, 'djvu')
    assert 'DJVU READING PAGE 1' in result.path.read_text(encoding='utf-8')
    if multiple:
        assert 'DJVU READING PAGE 2' in result.path.read_text(encoding='utf-8')
    assert len(result.report['segments']) == (2 if multiple else 1)
    prove(result, source, before)


def test_real_djvu_without_text_layer_is_not_reported_as_ocr(tmp_path):
    source = djvu(tmp_path, text=False)
    before = source.read_bytes()
    assert _djvu_pages(before, 1) == 1  # Accept actual cjb2 framing before refusing absent text.
    with pytest.raises(ConversionError, match='embedded text|fixed operation'):
        convert(tmp_path, source, 'djvu')
    assert source.read_bytes() == before and not list((tmp_path / 'reading-output').iterdir())


def test_real_djvu_external_reference_is_refused_before_adjacent_content_read(tmp_path):
    source = djvu(tmp_path)
    raw = source.read_bytes()
    name = b'../unselected.djvu'
    chunk = b'INCL' + struct.pack('>I', len(name)) + name + b'\0' * (len(name) & 1)
    source.write_bytes(raw[:8] + struct.pack('>I', len(raw) - 12 + len(chunk)) + raw[12:] + chunk)
    before = source.read_bytes()
    outside = source.parent.parent / 'unselected.djvu'
    outside.write_bytes(b'UNSELECTED PRIVATE CONTENT')
    with pytest.raises(ConversionError, match='references'):
        convert(tmp_path, source, 'djvu')
    assert source.read_bytes() == before and outside.read_bytes() == b'UNSELECTED PRIVATE CONTENT'
    assert not list((tmp_path / 'reading-output').iterdir())


def test_real_chm_static_toc_order_does_not_execute_or_read_active_content(tmp_path):
    source = tmp_path / 'Синтетична довідка.chm'
    source.write_bytes(FIXTURES['sample_chm']())
    before = source.read_bytes()
    listing = subprocess.run([str(selected('chm')), 'l', '-slt', '-ba', '-sccUTF-8',
        '-tchm', '--', str(source)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=20, check=True)
    print('SYNTHETIC_CHM_NATIVE_LISTING', listing.stdout.decode('utf-8', 'replace')[:12000], flush=True)
    result = convert(tmp_path, source, 'chm')
    text = result.path.read_text(encoding='utf-8')
    assert text.index('FIRST CHM READING PAGE') < text.index('SECOND CHM READING PAGE')
    assert 'ACTIVE_CONTENT_MUST_NOT_APPEAR' not in text
    prove(result, source, before)


def _diagnose_public_chm(source, stage, monkeypatch):
    """Census every pinned public HTML member; never diagnose user content."""
    import json
    import time
    from carr_library.conversion import legacy
    from carr_library.extract import document_formats_text as html
    deadline = time.monotonic() + 45
    rows, checked, failures, excerpts = [], [], [], 0
    current = {'member': None, 'context': None}
    def emit(value):
        print('NATIVE_DIAGNOSTIC ' + json.dumps({
            'family': 'chm', 'known_public_publisher_fixture': True,
            'reproduction_only': True, **value}, ensure_ascii=True, sort_keys=True), flush=True)
    def read(args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Publisher HTML diagnostic deadline exceeded.')
        child = subprocess.run([str(selected('chm')), *map(str, args)],
            cwd=stage, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=min(5, remaining), check=True)
        if len(child.stdout) > legacy.MAX_MEMBER_BYTES:
            raise ValueError('Publisher HTML diagnostic output exceeded its bound.')
        return child.stdout
    parser = html._HTMLPreview
    class Trace(parser):
        def report(self, label, tag=None):
            current['context'] = {'stage': label, 'tag': tag, 'position': self.getpos(),
                'open_elements': [entry[0] for entry in self.stack][-128:]}
        def handle_starttag(self, tag, attrs):
            try:
                return super().handle_starttag(tag, attrs)
            except ValueError:
                self.report('start', tag)
                raise
        def handle_endtag(self, tag):
            try:
                return super().handle_endtag(tag)
            except ValueError:
                self.report('end', tag)
                raise
        def finish(self):
            try:
                return super().finish()
            except ValueError:
                self.report('finish')
                raise
    complete = False
    try:
        inventory = legacy._chm_listing(read(['l', '-slt', '-ba', '-sccUTF-8', '-tchm', '--', source]),
                                       128 * 1024**2)
        rows = [row for row in sorted(inventory, key=lambda row: row['name'])
                if not row['directory'] and Path(row['name']).suffix.lower() in ('.html', '.htm', '.xhtml')]
        with monkeypatch.context() as patch:
            patch.setattr(html, '_HTMLPreview', Trace)
            for row in rows:
                if not 0 < row['bytes'] <= legacy.MAX_MEMBER_BYTES:
                    raise ValueError('Publisher HTML diagnostic member exceeded its bound.')
                raw = read(['x', '-so', '-y', '-spd', '-sccUTF-8', '-tchm', '--', source, row['name']])
                if len(raw) != row['bytes']:
                    raise ValueError('Publisher HTML member changed from its exact inventory.')
                current.update(member=row['name'], context=None)
                observed = {'member': row['name'], 'bytes': len(raw), 'sha256': sha256(raw).hexdigest()}
                try:
                    reading = legacy._chm_html(raw, deadline=deadline)
                    legacy.extract_document(reading, '.html', max_bytes=legacy.MAX_MEMBER_BYTES,
                        max_chars=10_000_000, deadline=deadline)
                    observed.update(status='PASS', parsed_sha256=sha256(reading).hexdigest())
                except (ValueError, ConversionError) as error:
                    observed.update(status='FAIL', error_type=type(error).__name__,
                        reason=str(error)[:1000], context=current['context'])
                    # A bounded exact-source window from redistributable help
                    # distinguishes genuinely orphan tokens from parser effects.
                    if excerpts < 32 and current['context'] is not None:
                        budget = html._Budget(10_000_000, None, deadline)
                        text = html._decode(raw, budget, html._html_encoding(raw))
                        line = current['context']['position'][0]
                        lines = text.splitlines(keepends=True)
                        excerpt = ''.join(lines[max(0, line - 3):line + 2])
                        observed['source_excerpt'] = excerpt[:2048]
                        observed['source_excerpt_first_line'] = max(1, line - 2)
                        observed['encoding'] = next(iter(budget.encodings))
                        excerpts += 1
                    failures.append(observed)
                    emit({'event': 'member-failure', **observed})
                checked.append(observed)
        complete = True
    finally:
        emit({'event': 'publisher-html-census', 'complete': complete,
            'declared_html_members': len(rows), 'checked_html_members': len(checked),
            'failed_members': len(failures), 'members': checked,
            'seconds_budget': 45})
    if failures:
        raise ValueError('Pinned public CHM HTML census found ' + str(len(failures)) + ' parser failures.')


def test_real_chm_public_publisher_help_exercises_compressed_container(tmp_path, monkeypatch):
    supplied = Path(os.environ['CARR_7ZIP_HELP'])
    assert supplied.is_file() and supplied.resolve().is_relative_to(selected('chm').parent.resolve())
    source = tmp_path / 'Publisher help.chm'
    source.write_bytes(supplied.read_bytes())
    before = source.read_bytes()
    try:
        result = convert(tmp_path, source, 'chm')
    except ConversionError:
        _diagnose_public_chm(source, tmp_path, monkeypatch)
        raise
    assert '7-Zip' in result.path.read_text(encoding='utf-8')
    assert len(result.report['segments']) > 2
    prove(result, source, before)


def test_real_chm_output_budget_keeps_original_and_empty_destination(tmp_path):
    source = tmp_path / 'bounded.chm'
    source.write_bytes(FIXTURES['sample_chm']())
    before = source.read_bytes()
    with pytest.raises(ConversionError, match='budget|limit'):
        convert(tmp_path, source, 'chm', budget=ConversionBudget(max_text_bytes=4))
    assert source.read_bytes() == before and not list((tmp_path / 'reading-output').iterdir())


def test_real_chm_unsafe_member_inventory_is_refused_before_extracting_html(tmp_path):
    source = tmp_path / 'unsafe.chm'
    source.write_bytes(FIXTURES['chm']({'../unselected.htm': b'<p>UNSELECTED FILE</p>'}))
    before = source.read_bytes()
    with pytest.raises(ConversionError, match='unsafe|ambiguous'):
        convert(tmp_path, source, 'chm')
    assert source.read_bytes() == before and not list((tmp_path / 'reading-output').iterdir())
