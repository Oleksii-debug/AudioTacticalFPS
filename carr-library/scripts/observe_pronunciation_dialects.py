"""Bounded authored dictionary observations using the publisher's text utility.

Research only. No speech engine, binary redistribution or application integration.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import importlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import time
import urllib.request
import zipfile

URL = 'https://www.cross-plus-a.com/blb2txt.zip'
CORPUS = 'cat Cat CAT scat cats cat1 1cat cat_cat _cat cat_ écat caté αcat catα 猫cat cat猫 😀cat cat😀 (cat) cat.\r\ncat'

def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()

def prepare(root):
    root.mkdir(parents=True, exist_ok=False)
    request = urllib.request.Request(URL, headers={'User-Agent': 'Resonance-format-research/1'})
    blocks, total = [], 0
    deadline = time.monotonic() + 180
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.geturl() != URL:
            raise ValueError('The explicit official archive URL redirected.')
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError('The official archive download exceeded its deadline.')
            data = response.read(min(1024 * 1024, 64 * 1024**2 - total + 1))
            if not data:
                break
            total += len(data)
            if total > 64 * 1024**2:
                raise ValueError('The official archive exceeds its byte bound.')
            blocks.append(data)
    raw = b''.join(blocks)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        if len(members) > 512:
            raise ValueError('The official archive has too many entries.')
        choices = [entry for entry in members if entry.filename == 'blb2txt.exe']
        print(json.dumps({'archive_members': [{'name': item.filename[:240], 'bytes': item.file_size}
              for item in members]}, ensure_ascii=False), flush=True)
        if len(choices) != 1:
            raise ValueError('Expected the exact root blb2txt.exe from the official archive.')
        eligible = []
        for candidate in choices:
            candidate_mode = candidate.external_attr >> 16
            if (candidate.is_dir() or stat.S_ISLNK(candidate_mode) or candidate.flag_bits & 1
                    or not 0 < candidate.file_size <= 32 * 1024**2):
                raise ValueError('An explicit text executable violates the extraction bound.')
            with archive.open(candidate) as stream:
                header = stream.read(min(candidate.file_size, 1024 * 1024))
            if len(header) < 64 or header[:2] != b'MZ':
                raise ValueError('An explicit text executable has no bounded PE header.')
            pe_offset = int.from_bytes(header[60:64], 'little')
            if not 64 <= pe_offset <= len(header) - 24 or header[pe_offset:pe_offset + 4] != bytes((80, 69, 0, 0)):
                raise ValueError('An explicit text executable has no valid bounded PE signature.')
            machine = int.from_bytes(header[pe_offset + 4:pe_offset + 6], 'little')
            if machine not in (0x14c, 0x8664):
                raise ValueError('The text executable has an unsupported machine architecture.')
            eligible.append((candidate, machine))
        # The recorded official ZIP also has a separate Without PDF build.
        # Select only the author's exact default root member, never that variant.
        entry = eligible[0][0]
        mode = entry.external_attr >> 16
        if (entry.is_dir() or stat.S_ISLNK(mode) or entry.flag_bits & 1
                or not 0 < entry.file_size <= 32 * 1024**2):
            raise ValueError('The selected text executable violates the extraction bound.')
        with archive.open(entry) as stream:
            executable = stream.read(32 * 1024**2 + 1)
        if len(executable) != entry.file_size:
            raise ValueError('The selected executable size changed.')
    selected = root / 'blb2txt.exe'
    selected.write_bytes(executable)
    record = dict(schema=1, official_url=URL, downloaded_at=datetime.now(timezone.utc).isoformat(),
                  archive_sha256=sha256(raw).hexdigest(), archive_bytes=len(raw),
                  archive_member=entry.filename, executable_machine=hex(eligible[0][1]),
                  executable_sha256=digest(selected), executable_bytes=selected.stat().st_size,
                  use='authored format research only; no binary distribution')
    (root / 'selected.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(record, ensure_ascii=False), flush=True)

def bxd(rows):
    return '\ufeffVersion=1\r\nDelimiter=\x03\r\nAuthored probe\x03Resonance\x03Synthetic rules only\r\n' + ''.join('\x03'.join(row) + '\r\n' for row in rows)

def cases():
    base = ['A'*32, '1', '1', '0', '0', '0', '0', 'cat', 'TOKEN', '']
    result = [('baseline', None, None, CORPUS)]
    result.append(('bxd-default', 'bxd', bxd([base]), CORPUS))
    for field, values in ((2, range(3)), (3, range(3)), (5, range(4)), (6, range(4))):
        for value in values:
            row = list(base)
            row[field] = str(value)
            result.append((f'bxd-field-{field}-{value}', 'bxd', bxd([row]), CORPUS))
    for left in (0, 1):
        for right in (0, 1):
            row = list(base)
            row[5:7] = [str(left), str(right)]
            result.append((f'bxd-boundaries-{left}-{right}', 'bxd', bxd([row]), CORPUS))
    for regular in (0, 1):
        row = list(base)
        row[4], row[7] = str(regular), 'c.t'
        result.append((f'bxd-type-{regular}', 'bxd', bxd([row]), 'cat c.t cot CAT'))
    row = list(base)
    row[4], row[7], row[8] = '1', '(cat)(s?)', '$2-$1-$0'
    result.append(('bxd-captures', 'bxd', bxd([row]), 'cat cats CAT CATS'))
    one, two = list(base), list(base)
    one[1], one[7], one[8] = '9', 'A', 'B'
    two[0], two[1], two[7], two[8] = 'B'*32, '1', 'B', 'C'
    result.append(('bxd-ordinal-versus-physical', 'bxd', bxd([one, two]), 'A'))
    for pattern in ('cat', '$cat', '*cat', 'cat*', '*cat*'):
        result.append(('dic-' + str(len(result)), 'dic', '\ufeff' + pattern + '=X\r\n', CORPUS))
    for rule in ('**=STAR', '$$=DOLLAR', '===EQUAL', '##=HASH'):
        result.append(('dic-literal-' + str(len(result)), 'dic', '\ufeff' + rule + '\r\n', 'a*b * $ a$b = a=b # a#b'))
    result.append(('dic-longest-order', 'dic', '\ufeffcat=X\r\ncatalog=Y\r\n', 'catalog cat'))
    result.append(('dic-case-order', 'dic', '\ufeff$cat=X\r\ncatalog=Y\r\n', 'catalog cat Catalog CAT'))
    for rule in ('cat=X', '@cat=X', '(cat)(s?)=$2-$1-$0', 'a\\=b=EQUAL', 'cat=\\U$0', 'cat=$$'):
        result.append(('rex-' + str(len(result)), 'rex', '\ufeff' + rule + '\r\n', 'cat cats CAT a=b'))
    result.append(('rex-order', 'rex', '\ufeffA=B\r\nB=C\r\n', 'A'))
    if len(result) > 64:
        raise AssertionError('Authored case count exceeded its fixed budget.')
    return result

def run(root, report):
    if os.name != 'nt' or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError('Run actual Windows installed code with -I -B.')
    from carr_library.audio.models import AudioBudget
    from carr_library.audio.process import run_process
    record = json.loads((root / 'selected.json').read_text(encoding='utf-8'))
    executable = root / 'blb2txt.exe'
    if (digest(executable) != record['executable_sha256']
            or executable.stat().st_size != record['executable_bytes']):
        raise ValueError('The selected official executable changed.')
    source_root = Path(__file__).resolve().parents[1] / 'src'
    origins = {}
    for name in ('carr_library.audio.process', 'carr_library.audio.models', 'carr_library.audio.windows_job'):
        module = importlib.import_module(name)
        installed = Path(module.__file__).resolve()
        expected = source_root / Path(*name.split('.')).with_suffix('.py')
        if installed.is_relative_to(source_root) or digest(installed) != digest(expected):
            raise AssertionError('Research process owner differs from installed source: ' + name)
        origins[name] = dict(path=str(installed), sha256=digest(installed))
    started = time.monotonic()
    rows = []
    for label, kind, rules, text in cases():
        if time.monotonic() - started >= 300:
            raise TimeoutError('The complete authored observation set exceeded five minutes.')
        case = root / label
        case.mkdir(exist_ok=False)
        input_path, output, config = case / 'input.txt', case / 'result.txt', case / 'empty.cfg'
        input_path.write_text('\ufeff' + text, encoding='utf-8', newline='')
        config.write_bytes(b'')
        retained = [input_path, config]
        command = [str(executable), '-f', str(input_path), '-out', str(output), '-e', 'utf8', '-cfg', str(config)]
        if kind is not None:
            dictionary = case / ('rules.' + kind)
            dictionary.write_text(rules, encoding='utf-8', newline='')
            retained.append(dictionary)
            command += ['-d', str(dictionary)]
        before = {p.name: digest(p) for p in retained}
        remaining = 300 - (time.monotonic() - started)
        if remaining <= .05:
            raise TimeoutError('No native execution time remains in this authored observation set.')
        result = run_process(command, budget=AudioBudget(timeout=min(10, remaining), max_output_bytes=1024**2,
                             max_log_bytes=32768, reserve_disk_bytes=0, threads=1),
                             cwd=case, owned_tree=True, watch=(case,), allow_failure=True)
        if before != {p.name: digest(p) for p in retained}:
            raise AssertionError('A native observation modified its authored inputs.')
        rendered = None
        if output.exists():
            if not output.is_file() or output.stat().st_size > 8192:
                raise ValueError('A native observation output exceeds its explicit text budget.')
            rendered = output.read_bytes().decode('utf-8-sig')
        row = dict(case=label, kind=kind, input=text, dictionary=rules,
                   exit_code=result.returncode, output=rendered,
                   stdout=result.stdout.decode('utf-8', errors='replace')[:2048],
                   stderr=result.stderr.decode('utf-8', errors='replace')[:2048])
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    if digest(executable) != record['executable_sha256']:
        raise AssertionError('The selected publisher executable changed after execution.')
    result = dict(schema=1, status='OBSERVED', selection=record, source_identity=origins,
                  cases=rows, native_process_family='owned per call', inference='NOT_RUN',
                  note='Raw authored observations; a dialect claim requires reviewing the exact results.')
    raw = json.dumps(result, ensure_ascii=False, indent=2)
    if len(raw.encode('utf-8')) > 256 * 1024:
        raise ValueError('The text-only research evidence exceeds its byte budget.')
    with report.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(raw + '\n')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'run'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    root = args.root.absolute()
    if args.action == 'prepare':
        prepare(root)
    else:
        if args.report is None:
            parser.error('run requires a new text-only report path')
        run(root, args.report.absolute())

if __name__ == '__main__':
    main()
