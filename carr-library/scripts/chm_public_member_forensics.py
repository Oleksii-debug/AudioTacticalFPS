"""Read-only diagnosis of two exact public CHM members; never release qualification."""
from __future__ import annotations

import ast
from hashlib import sha256
import importlib.util
import itertools
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
RESULTS = PROJECT / 'ci-results'
LOCK_HASH = '527195c188d7d0af748cd48d220ab8cdc5cb99be3d49ac4d9be7324d8abf9bc0'
PIN = {
    'version': '26.06',
    'url': 'https://github.com/ip7z/7zip/releases/download/26.06/7z2606-x64.msi',
    'bytes': 2003456,
    'sha256': '6dc822569e777d9a3ecd5ffcbc22b55600af87e6c5d9810a64e8566cf5a4af9f',
}
MEMBERS = {
    'cmdline/switches/index.htm': (7746, '38308f9d9bbdf73d55b19facdd13f4fe97e30a89141aa08aabe9c7ae0de064e8'),
    'fm/plugins/7-zip/add.htm': (16035, 'b6bff175f02f13a83cd585b3b959afa634b85bec8a082d6331ac3663cce41fb0'),
}
SOURCES = {
    'src/carr_library/conversion/legacy.py': 'f2bbe7058f6c267d00930d3910b01067a4e7ae12454f72313b6d1b59384ff826',
    'src/carr_library/extract/document_formats_text.py': '0e81fa6b2c22e8ff1745fc474484cf879fdf6b1c3fcd69d804f1bf78d1e78d7f',
    'src/carr_library/conversion/models.py': '731955037700041573bea0b428dffee6ff02c6587389c6a184828a914a0959ca',
}

def prepare():
    original = (PROJECT / 'packaging/windows-cp311.lock').read_text(encoding='utf-8')
    # Inspect the complete existing requirement, not another package's hashes.
    lines = original.splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith('lxml=='))
    last = first + 1
    while last < len(lines) and not re.match(r'^[A-Za-z0-9_-]+==', lines[last]):
        last += 1
    selected = '\n'.join(lines[first:last])
    if not selected.startswith('lxml==6.1.3 ') or '--hash=sha256:' + LOCK_HASH not in selected:
        raise AssertionError('The existing reviewed lxml wheel pin changed.')
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / 'chm-forensics-lxml.lock').write_text(
        'lxml==6.1.3 --hash=sha256:' + LOCK_HASH + '\n', encoding='ascii')
    print(json.dumps({'kind': 'existing-lxml-pin', 'version': '6.1.3',
                      'wheel_sha256': LOCK_HASH, 'new_product_dependencies': False}))

def command(args, cwd, *, maximum=8 * 1024**2, timeout=30):
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen([str(arg) for arg in args], cwd=cwd,
            stdin=subprocess.DEVNULL, stdout=output, stderr=errors, start_new_session=True,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'})
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if (time.monotonic() >= deadline or os.fstat(output.fileno()).st_size > maximum
                        or os.fstat(errors.fileno()).st_size > 65536):
                    raise RuntimeError('A diagnostic parser exceeded its fixed time or output bound.')
                time.sleep(.02)
            output.seek(0)
            raw = output.read(maximum + 1)
            errors.seek(0)
            diagnostic = errors.read(65537)
            if process.returncode or len(raw) > maximum or len(diagnostic) > 65536:
                raise RuntimeError('A diagnostic parser refused input: ' +
                                   diagnostic[:2000].decode('utf-8', 'replace'))
            return raw
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

def listing(executable, archive, work, inventory, depth):
    data = command([executable, 'l', '-slt', '-ba', '-sccUTF-8', '--', archive],
                   work, maximum=1024**2)
    rows = []
    for block in re.split(r'\r?\n[ \t]*\r?\n', data.decode('utf-8', 'strict')):
        fields = {}
        for line in block.splitlines():
            key, separator, value = line.partition(' = ')
            if separator:
                if key in fields:
                    raise ValueError('Duplicate archive listing field.')
                fields[key] = value
        if 'Path' not in fields or 'Size' not in fields:
            continue
        if not fields['Size'].isdigit():
            raise ValueError('A diagnostic member size is not an integer.')
        name, size = fields['Path'], int(fields['Size'])
        if len(name) > 1024 or '\x00' in name or size > 64 * 1024**2:
            raise ValueError('A diagnostic member exceeds its bounds.')
        rows.append({'name': name, 'bytes': size,
                     'directory': fields.get('Folder') == '+' or
                                  fields.get('Attributes', '').startswith('D')})
    if not rows or len(rows) > 512:
        raise ValueError('The diagnostic container has no bounded member inventory.')
    inventory.append({'depth': depth, 'container_bytes': archive.stat().st_size,
                      'container_sha256': sha256(archive.read_bytes()).hexdigest(),
                      'members': rows})
    return rows

def extract(executable, archive, row, work):
    raw = command([executable, 'x', '-so', '-y', '-spd', '-sccUTF-8', '--',
                   archive, row['name']], work, maximum=min(row['bytes'], 8 * 1024**2))
    if len(raw) != row['bytes']:
        raise ValueError('Extracted bytes differ from the exact archive listing.')
    return raw

def find_help(executable, archive, work, inventory, depth=0):
    if depth > 2:
        raise ValueError('Unexpected extra public installer archive nesting.')
    rows = listing(executable, archive, work, inventory, depth)
    direct = [r for r in rows if not r['directory'] and r['name'].lower().endswith('.chm')]
    if len(direct) > 1:
        raise ValueError('The publisher installer contains ambiguous help members.')
    if direct:
        raw = extract(executable, archive, direct[0], work)
        if raw[:8] != b'ITSF\x03\0\0\0':
            raise ValueError('The selected help file is not a CHM version-3 container.')
        target = work / 'publisher-help.chm'
        target.write_bytes(raw)
        return target
    cabinets = [r for r in rows if not r['directory'] and
                (r['name'].lower().endswith('.cab') or r['name'].startswith('#'))]
    if not 1 <= len(cabinets) <= 8:
        raise ValueError('No finite cabinet path found; inspect the recorded inert inventory.')
    found = []
    for number, row in enumerate(cabinets):
        if not 0 < row['bytes'] <= 8 * 1024**2:
            raise ValueError('A selected embedded cabinet exceeds its byte bound.')
        raw = extract(executable, archive, row, work)
        if raw[:4] != b'MSCF':
            continue
        cabinet = work / ('cabinet-' + str(depth) + '-' + str(number) + '.cab')
        cabinet.write_bytes(raw)
        found.append(find_help(executable, cabinet, work, inventory, depth + 1))
    if len(found) != 1:
        raise ValueError('The exact publisher help container was not unique.')
    return found[0]

def load_parser():
    for relative, expected in SOURCES.items():
        if sha256((PROJECT / relative).read_bytes()).hexdigest() != expected:
            raise AssertionError('The reviewed diagnostic source changed: ' + relative)
    preview_path = PROJECT / 'src/carr_library/extract/document_formats_text.py'
    spec = importlib.util.spec_from_file_location('_chm_forensic_preview', preview_path)
    preview = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = preview
    spec.loader.exec_module(preview)
    source = PROJECT / 'src/carr_library/conversion/legacy.py'
    parsed = ast.parse(source.read_text(encoding='utf-8'), filename=str(source))
    names = {'_chm_html', '_chm_paragraph_ends', '_chm_recovered_html'}
    constants = {'MAX_MEMBER_BYTES', '_LEGACY_HTML_DOCTYPES'}
    nodes = [node for node in parsed.body if
             isinstance(node, ast.FunctionDef) and node.name in names or
             isinstance(node, ast.Assign) and any(
                 isinstance(target, ast.Name) and target.id in constants for target in node.targets)]
    if len(nodes) != 5:
        raise AssertionError('The selected unchanged parser AST is incomplete.')
    models = ast.parse((PROJECT / 'src/carr_library/conversion/models.py').read_text(encoding='utf-8'))
    errors = [node for node in models.body if isinstance(node, ast.ClassDef) and node.name == 'ConversionError']
    if len(errors) != 1:
        raise AssertionError('The existing domain error type changed.')
    class ImportOnly(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            if node.level == 2 and node.module == 'extract.document_formats_text':
                return ast.copy_location(ast.ImportFrom(
                    module='_chm_forensic_preview', names=node.names, level=0), node)
            return node
    module = ImportOnly().visit(ast.Module(body=errors + nodes, type_ignores=[]))
    ast.fix_missing_locations(module)
    namespace = {'__name__': '_chm_forensic_legacy', 're': re, 'sha256': sha256, 'time': time}
    exec(compile(module, str(source), 'exec'), namespace)
    return namespace['_chm_html']

def token_rows(records):
    return [match.group() for record in records
            for match in re.finditer(r'\S+', record['raw_text'])]

def inspect_member(parser, name, raw):
    expected_bytes, expected_hash = MEMBERS[name]
    if len(raw) != expected_bytes or sha256(raw).hexdigest() != expected_hash:
        raise AssertionError('The selected public help member differs from the observed Windows fixture.')
    decoded_source = raw.decode('cp1252', 'strict')
    result = {'member': name, 'bytes': len(raw), 'sha256': expected_hash,
              'raw_header': '\n'.join(decoded_source.splitlines()[:26])[:2500]}
    try:
        recovered = parser(raw, max_chars=1_000_000, deadline=time.monotonic() + 15, notices=[])
        result.update(status='RETURNED', recovered_sha256=sha256(recovered).hexdigest())
    except Exception as error:
        result.update(status='REFUSED', error_type=type(error).__name__, reason=str(error)[:400])
        trace = error.__traceback__
        while trace is not None:
            code, local = trace.tb_frame.f_code, trace.tb_frame.f_locals
            if code.co_name == '_chm_recovered_html' and 'checked' in local:
                before, after = token_rows(local['before'].records), token_rows(local['checked'].records)
                changed = [index for index, pair in enumerate(itertools.zip_longest(before, after))
                           if pair[0] != pair[1]]
                first = changed[0] if changed else None
                result['ordered_tokens'] = {'before_count': len(before), 'after_count': len(after),
                    'first_difference': first,
                    'before_window': before[max(0, (first or 0) - 5):(first or 0) + 9],
                    'after_window': after[max(0, (first or 0) - 5):(first or 0) + 9]}
                if first is not None:
                    needle = before[first] if first < len(before) else after[first]
                    decoded = raw.decode('cp1252', 'strict')
                    position = decoded.find(needle)
                    result['source_context'] = decoded[max(0, position - 250):position + 500] if position >= 0 else None
                result['serialization_header'] = '\n'.join(
                    local.get('serialized', b'').decode('ascii', 'strict').splitlines()[:26])[:2500]
                result['serialization_context'] = local.get('serialized', b'')[:0].decode('ascii')
                if first is not None and 'serialized' in local:
                    value = local['serialized'].decode('ascii', 'strict')
                    needle = after[first] if first < len(after) else before[first]
                    position = value.find(needle)
                    result['serialization_context'] = value[max(0, position - 250):position + 500] if position >= 0 else None
            if code.co_name == '_chm_recovered_html' and 'strict' in local:
                strict = local['strict']
                line, column = strict.getpos()
                result['strict_failure_position'] = [line, column]
                result['strict_open_elements'] = [item[0] for item in strict.stack]
                lines = local.get('reading', '').splitlines()
                result['strict_failure_context'] = '\n'.join(
                    lines[max(0, line - 18):line + 6])[:4000]
                result['definition_tag_trace'] = [
                    {'line': number, 'tags': re.findall(r'</?(?:dl|dt|dd)\b[^>]*>', value,
                                                       flags=re.IGNORECASE)}
                    for number, value in enumerate(lines[:line + 6], 1)
                    if re.search(r'</?(?:dl|dt|dd)\b', value, flags=re.IGNORECASE)][-32:]
            if code.co_name == '_chm_paragraph_ends' and 'parser' in local:
                current = local['parser']
                line, column = current.getpos()
                result['parser_position'] = [line, column]
                result['open_elements'] = [item[0] for item in current.stack]
                result['failure_context'] = '\n'.join(raw.decode('cp1252').splitlines()[max(0, line - 3):line + 2])[:1000]
            trace = trace.tb_next
    return result

def main():
    RESULTS.mkdir(exist_ok=True)
    report = {'schema': 1, 'status': 'INCOMPLETE', 'kind': 'two-public-chm-members',
              'qualification': 'NOT_WINDOWS_OR_PRODUCT_QUALIFICATION',
              'source_commit': os.environ.get('GITHUB_SHA'), 'source_sha256': SOURCES,
              'archive_inventory': [], 'members': []}
    failure = None
    try:
        head = command(['/usr/bin/git', 'rev-parse', 'HEAD'], PROJECT, maximum=128).decode('ascii').strip()
        if head != report['source_commit']:
            raise AssertionError('The checked-out source does not match this exact workflow head.')
        report['source_tree'] = command(['/usr/bin/git', 'rev-parse', 'HEAD^{tree}'],
                                       PROJECT, maximum=128).decode('ascii').strip()
        pin = json.loads((PROJECT / 'packaging/legacy-conversion-runtimes.json').read_text())['seven_zip']
        if {key: pin[key] for key in PIN} != PIN:
            raise AssertionError('The already reviewed public publisher pin changed.')
        from lxml import etree
        if etree.LXML_VERSION[:3] != (6, 1, 3):
            raise AssertionError('Wrong existing lxml version.')
        report['lxml'] = {'version': list(etree.LXML_VERSION), 'libxml': list(etree.LIBXML_VERSION),
                          'wheel_sha256': LOCK_HASH}
        executable = next((path.resolve() for path in
            (Path('/usr/bin/7z'), Path('/usr/bin/7zz'), Path('/bin/7z'), Path('/bin/7zz'))
            if path.is_file()), None)
        if executable is None:
            raise RuntimeError('No fixed host system 7-Zip parser is available.')
        report['host_parser'] = {'path': str(executable),
            'sha256': sha256(executable.read_bytes()).hexdigest(),
            'scope': 'Host diagnostic archive parser only; no extracted program is executed.'}
        with tempfile.TemporaryDirectory(prefix='carr-chm-forensics-', dir=os.environ.get('RUNNER_TEMP')) as folder:
            work = Path(folder)
            version = command([executable, 'i'], work, maximum=65536)
            report['host_parser']['version_output_sha256'] = sha256(version).hexdigest()
            report['host_parser']['version_excerpt'] = version.decode('utf-8', 'replace')[:800]
            with urllib.request.urlopen(urllib.request.Request(PIN['url'],
                    headers={'User-Agent': 'CARR-public-fixture-forensics'}), timeout=30) as response:
                data = response.read(PIN['bytes'] + 1)
            if len(data) != PIN['bytes'] or sha256(data).hexdigest() != PIN['sha256']:
                raise AssertionError('Exact public installer size/hash mismatch.')
            report['publisher'] = PIN
            msi = work / 'publisher.msi'
            msi.write_bytes(data)
            chm = find_help(executable, msi, work, report['archive_inventory'])
            rows = listing(executable, chm, work, report['archive_inventory'], 3)
            selected = {}
            for row in rows:
                name = row['name'].replace('\\', '/').lstrip('/')
                if name in MEMBERS:
                    if name in selected or row['directory'] or row['bytes'] != MEMBERS[name][0]:
                        raise ValueError('Ambiguous or changed exact public HTML member.')
                    selected[name] = row
            if set(selected) != set(MEMBERS):
                raise ValueError('The two exact public members are unavailable.')
            parser = load_parser()
            for name, row in selected.items():
                report['members'].append(inspect_member(parser, name, extract(executable, chm, row, work)))
        report['temporary_payload_removed'] = True
        report['status'] = 'DIAGNOSTIC_COMPLETE'
    except Exception as error:
        failure = error
        report.update(status='DIAGNOSTIC_FAILED', error_type=type(error).__name__, reason=str(error)[:2000])
    finally:
        output = json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2) + '\n'
        if len(output.encode('utf-8')) > 256 * 1024:
            raise ValueError('The inert diagnostic report exceeds its byte limit.')
        (RESULTS / 'chm-forensics.json').write_text(output, encoding='utf-8')
        print('CHM_FORENSICS ' + json.dumps(report, ensure_ascii=True, sort_keys=True), flush=True)
    return 1 if failure is not None else 0

if __name__ == '__main__':
    if sys.argv[1:] == ['--prepare']:
        prepare()
    elif sys.argv[1:]:
        raise SystemExit('This diagnostic accepts no source paths or arbitrary URL arguments.')
    else:
        raise SystemExit(main())
