"""Isolated Windows diagnostic for the exact 4c14 state publisher.

This is a synthetic probe, not a replacement publisher or application gate.
Only the terminated variant changes the allocation by one zeroed WCHAR.
The original variant is observational: no particular failure is assumed.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import traceback


BASE_COMMIT = '4c14aaab538578256336bb66639ceb67eea5c9d4'
STATE_SHA256 = 'b3cea74bebfcedd20637ff760d3a8201794be837b651ffa6fa71f88425ae2136'
STATE_PATH = Path(__file__).resolve().parents[1] / 'src/carr_library/durable_state.py'
OLD = b'{"done":0}'
NEW = b'{"done":1}\n'
SENTINEL = '-past-end\0'.encode('utf-16-le')


def load_state():
    raw = STATE_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != STATE_SHA256:
        raise RuntimeError('The diagnostic requires exact 4c14 durable_state.py.')
    spec = importlib.util.spec_from_file_location('exact_diagnostic_state', STATE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inventory(directory):
    result = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        entry = {'name': path.name, 'name_repr': repr(path.name),
                 'name_utf16le_hex': path.name.encode('utf-16-le', errors='surrogatepass').hex()}
        try:
            info = path.lstat()
            raw = path.read_bytes()
            entry.update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                         bytes_hex=raw.hex(), inode=info.st_ino, nlink=info.st_nlink)
        except OSError as error:
            entry.update(error=repr(error), winerror=getattr(error, 'winerror', None))
        result.append(entry)
    return result


def child(target, variant, allocator):
    state = load_state()
    source = inspect.getsource(state._replace_file)
    original_line = 'size = RenameInfo.FileName.offset + len(filename)'
    if source.count(original_line) != 1:
        raise RuntimeError('The frozen allocation preimage does not match.')
    if variant == 'terminated':
        source = source.replace(original_line, original_line + ' + ctypes.sizeof(wintypes.WCHAR)')
        exec(compile(source, '<diagnostic-only-zero-WCHAR-variant>', 'exec'), state.__dict__)

    record = {'variant': variant, 'allocator': allocator, 'published': False,
              'target': str(target), 'target_utf16le_hex': str(target).encode('utf-16-le').hex(),
              'state_sha256': STATE_SHA256, 'replace_source': source, 'calls': []}
    real_allocator = ctypes.create_string_buffer
    real_library = ctypes.WinDLL
    real_replace = os.replace
    allocations = []

    def allocate(size):
        if not isinstance(size, int):
            raise TypeError('This probe only expects the integer allocation overload.')
        if allocator == 'sentinel':
            backing = real_allocator(size + len(SENTINEL))
            ctypes.memmove(ctypes.addressof(backing) + size, SENTINEL, len(SENTINEL))
            view = (ctypes.c_char * size).from_buffer(backing)
        else:
            backing = view = real_allocator(size)
        # Both references survive the complete native call. The native API still
        # receives the requested size, never the larger sentinel backing size.
        allocations.append((size, view, backing))
        return view

    class FunctionTrace:
        def __init__(self, name, function):
            object.__setattr__(self, 'name', name)
            object.__setattr__(self, 'function', function)

        def __setattr__(self, name, value):
            setattr(self.function, name, value)

        def __call__(self, *args):
            event = {'api': self.name}
            if self.name == 'SetFileInformationByHandle':
                event.update(information_class=args[1], dwBufferSize=args[3],
                             requested_bytes_hex=ctypes.string_at(args[2], args[3]).hex())
            elif self.name == 'CreateFileW':
                event.update(path=args[0], access=args[1], sharing=args[2],
                             disposition=args[4], flags=args[5])
            result = self.function(*args)
            # Capture immediately; diagnostics must not replace the native result.
            error = ctypes.get_last_error()
            event.update(result=result, last_error=error)
            record['calls'].append(event)
            return result

    class KernelTrace:
        def __init__(self, kernel):
            self.functions = {name: FunctionTrace(name, getattr(kernel, name))
                              for name in ('CreateFileW', 'SetFileInformationByHandle', 'CloseHandle')}

        def __getattr__(self, name):
            return self.functions[name]

    def library(*args, **kwargs):
        return KernelTrace(real_library(*args, **kwargs))

    def replace(source_path, destination):
        event = {'api': 'os.replace', 'source': str(source_path), 'destination': str(destination)}
        try:
            result = real_replace(source_path, destination)
        except OSError as error:
            event.update(result=False, error=repr(error), winerror=getattr(error, 'winerror', None))
            record['calls'].append(event)
            raise
        event['result'] = True
        record['calls'].append(event)
        return result

    ctypes.create_string_buffer = allocate
    ctypes.WinDLL = library
    os.replace = replace
    exit_code = 0
    try:
        state.atomic_json(target, {'done': 1}, replace=True)
        record['published'] = True
    except OSError as error:
        record.update(error=repr(error), winerror=getattr(error, 'winerror', None))
        exit_code = 2
    except BaseException as error:
        record.update(error=repr(error), traceback=traceback.format_exc())
        exit_code = 3
    finally:
        ctypes.create_string_buffer = real_allocator
        ctypes.WinDLL = real_library
        os.replace = real_replace
    record['allocations'] = [
        {'requested_size': size, 'view_size': ctypes.sizeof(view),
         'backing_size': ctypes.sizeof(backing),
         'requested_bytes_hex': bytes(view).hex(),
         'tail_outside_requested_size_hex': bytes(backing)[size:].hex()}
        for size, view, backing in allocations]
    record['directory_after_publication'] = inventory(target.parent)
    print(json.dumps(record, ensure_ascii=True, sort_keys=True), flush=True)
    return exit_code


def one_case(root, state, reader, variant, allocator):
    directory = root / f'{reader}-{variant}-{allocator}'
    directory.mkdir()
    target = directory / 'стан 🌿.json'
    target.write_bytes(OLD)
    record = {'reader': reader, 'variant': variant, 'allocator': allocator,
              'directory': str(directory), 'before': inventory(directory)}
    open_reader = state._open_json_file if reader == 'shared' else lambda path: path.open('rb')
    with open_reader(target) as held:
        record['held_handle_inheritable'] = os.get_inheritable(held.fileno())
        process = subprocess.run(
            [sys.executable, '-I', '-B', '-X', 'utf8', str(Path(__file__).resolve()),
             '--child', str(target), '--variant', variant, '--allocator', allocator],
            capture_output=True, text=True, encoding='utf-8', timeout=30)
        record.update(child_returncode=process.returncode, child_stderr=process.stderr,
                      child_stdout=process.stdout)
        try:
            record['child'] = json.loads(process.stdout)
        except (ValueError, TypeError):
            record['child'] = {'diagnostic_error': 'The child did not emit one JSON record.'}
        record['held_bytes_hex'] = held.read().hex()
        record['directory_while_held'] = inventory(directory)
        record['fresh_bytes_while_held_hex'] = target.read_bytes().hex()
    record['held_closed'] = held.closed
    record['directory_after_close'] = inventory(directory)
    record['fresh_bytes_after_close_hex'] = target.read_bytes().hex()
    record['fresh_production_json'] = state.read_json(target)
    return record


def validate_case(record):
    errors = []
    label = '/'.join(record[key] for key in ('reader', 'variant', 'allocator'))
    if record['child_returncode'] not in (0, 2) or 'diagnostic_error' in record['child']:
        errors.append(f'{label}: the diagnostic child itself failed.')
    if record['held_bytes_hex'] != OLD.hex() or not record['held_closed']:
        errors.append(f'{label}: the original held snapshot was not preserved and closed.')
    # Original publication is evidence, not an expected failure or acceptance gate.
    if record['variant'] == 'terminated':
        child_record = record['child']
        published = child_record.get('published')
        if record['reader'] == 'shared' and published is not True:
            errors.append(f'{label}: the terminated publisher did not publish with a shared reader.')
        if not published and (record['reader'] != 'ordinary'
                              or child_record.get('winerror') not in (5, 32, 33)):
            errors.append(f'{label}: refusal was not the documented ordinary-reader sharing conflict.')
        expected = NEW if published else OLD
        if any(record[key] != expected.hex() for key in
               ('fresh_bytes_while_held_hex', 'fresh_bytes_after_close_hex')):
            errors.append(f'{label}: fresh destination bytes disagree with the publication result.')
        if record['fresh_production_json'] != {'done': int(bool(published))}:
            errors.append(f'{label}: the production reader disagrees with the publication result.')
        for key in ('directory_while_held', 'directory_after_close'):
            if [entry['name'] for entry in record[key]] != ['стан 🌿.json']:
                errors.append(f'{label}: unexpected renamed or staging file in {key}.')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child', type=Path)
    parser.add_argument('--variant', choices=('original', 'terminated'))
    parser.add_argument('--allocator', choices=('normal', 'sentinel'))
    parser.add_argument('--report', type=Path, default=Path('state-rename-diagnostic.json'))
    args = parser.parse_args()
    if os.name != 'nt':
        raise SystemExit('This diagnostic requires actual Windows; no portable PASS is claimed.')
    if args.child:
        if args.variant is None or args.allocator is None:
            parser.error('The child requires an explicit variant and allocator.')
        return child(args.child, args.variant, args.allocator)
    state = load_state()
    report = {'base_commit': BASE_COMMIT, 'diagnostic_commit': os.environ.get('GITHUB_SHA'),
              'state_sha256': STATE_SHA256, 'python': sys.version, 'executable': sys.executable,
              'platform': platform.platform(), 'wchar_size': ctypes.sizeof(wintypes.WCHAR),
              'pointer_size': ctypes.sizeof(ctypes.c_void_p),
              'probe_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'cases': [], 'errors': []}
    with tempfile.TemporaryDirectory(prefix='carr-state-rename-') as temporary:
        for reader in ('ordinary', 'shared'):
            for variant in ('original', 'terminated'):
                for allocator in ('normal', 'sentinel'):
                    try:
                        record = one_case(Path(temporary), state, reader, variant, allocator)
                        report['cases'].append(record)
                        report['errors'].extend(validate_case(record))
                        print(json.dumps(record, ensure_ascii=True, sort_keys=True), flush=True)
                    except BaseException as error:
                        failure = {'reader': reader, 'variant': variant, 'allocator': allocator,
                                   'error': repr(error), 'traceback': traceback.format_exc()}
                        report['cases'].append(failure)
                        report['errors'].append(failure)
                        print(json.dumps(failure, ensure_ascii=True), flush=True)
    report['status'] = 'PASS' if not report['errors'] and len(report['cases']) == 8 else 'FAIL'
    args.report.write_bytes((json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2) + '\n').encode('utf-8'))
    print(json.dumps({'status': report['status'], 'cases': len(report['cases']),
                      'errors': report['errors'], 'report': str(args.report)}, ensure_ascii=True), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
