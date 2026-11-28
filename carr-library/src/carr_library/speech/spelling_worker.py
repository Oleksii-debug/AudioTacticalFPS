"""Standalone Hunspell C API bridge; no application or third-party imports.

API reviewed at upstream 2969be996acad84b91ab3875b1816636fe61a40e.
Only explicitly selected, hash-verified native code is loaded in this child.
"""
import codecs
import ctypes
from hashlib import sha256
import json
import os
from pathlib import Path, PureWindowsPath
import stat
import sys


class ReviewError(Exception):
    pass


def regular(path):
    if not isinstance(path, str) or not path or '\0' in path or path.startswith(('\\\\', '//')):
        raise ReviewError('invalid')
    value = Path(path)
    if not value.is_absolute() or str(value.absolute()) != path:
        raise ReviewError('invalid')
    name = value.name
    if (not name or name.rstrip(' .') != name or PureWindowsPath(name).is_reserved()
            or any(c in '<>:"/\\|?*' or ord(c) < 32 for c in name)):
        raise ReviewError('invalid')
    for parent in (value.parent, *value.parent.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ReviewError('changed')
    info = value.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise ReviewError('changed')
    return value, info


def stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def stage(binding, target, maximum):
    if (type(binding) is not dict or set(binding) != {'path', 'size', 'sha256'}
            or type(binding['size']) is not int or not 0 < binding['size'] <= maximum
            or type(binding['sha256']) is not str or len(binding['sha256']) != 64
            or any(c not in '0123456789abcdef' for c in binding['sha256'])):
        raise ReviewError('invalid')
    path, before = regular(binding['path'])
    if before.st_size != binding['size']:
        raise ReviewError('changed')
    count, digest = 0, sha256()
    with path.open('rb') as source, target.open('xb') as output:
        if stamp(os.fstat(source.fileno())) != stamp(before):
            raise ReviewError('changed')
        while chunk := source.read(min(1024 * 1024, binding['size'] + 1 - count)):
            count += len(chunk)
            if count > maximum or count > binding['size']:
                raise ReviewError('changed')
            digest.update(chunk)
            output.write(chunk)
        if stamp(os.fstat(source.fileno())) != stamp(before):
            raise ReviewError('changed')
    if (count != binding['size'] or digest.hexdigest() != binding['sha256']
            or stamp(regular(binding['path'])[1]) != stamp(before)):
        raise ReviewError('changed')


def bind(path):
    try:
        # No current-directory/PATH DLL probing or unreviewed companion folder.
        library = ctypes.CDLL(str(path), **({'winmode': 0x1100} if os.name == 'nt' else {}))
        pointer, string = ctypes.c_void_p, ctypes.c_char_p
        strings = ctypes.POINTER(string)
        for name, args, result in (
                ('Hunspell_create', [string, string], pointer),
                ('Hunspell_destroy', [pointer], None),
                ('Hunspell_get_dic_encoding', [pointer], string),
                ('Hunspell_spell', [pointer, string], ctypes.c_int),
                ('Hunspell_suggest', [pointer, ctypes.POINTER(strings), string], ctypes.c_int),
                ('Hunspell_free_list', [pointer, ctypes.POINTER(strings), ctypes.c_int], None)):
            function = getattr(library, name)
            function.argtypes, function.restype = args, result
        return library
    except (AttributeError, OSError, ValueError) as exc:
        raise ReviewError('unavailable') from exc


def dictionary_encoding(root):
    declarations = []
    with (root / 'dictionary.aff').open('rb') as stream:
        for line in stream:
            if len(line) > 65536 or b'\0' in line:
                raise ReviewError('dictionary')
            fields = line.split()
            if fields and fields[0] == b'SET':
                if len(fields) != 2:
                    raise ReviewError('dictionary')
                declarations.append(fields[1].decode('ascii'))
    if len(declarations) != 1:
        raise ReviewError('dictionary')
    try:
        encoding = codecs.lookup(declarations[0]).name
    except LookupError as exc:
        raise ReviewError('dictionary') from exc
    with (root / 'dictionary.dic').open('rb') as stream:
        header = stream.readline(64).strip()
    if not header.isdigit() or not 0 < int(header) <= 10_000_000:
        raise ReviewError('dictionary')
    return encoding


def review(raw):
    data = json.loads(raw)
    if (type(data) is not dict or set(data) != {'profile', 'words', 'staging'}
            or type(data['profile']) is not dict
            or set(data['profile']) != {'library', 'affix', 'dictionary', 'language', 'provenance'}
            or type(data['words']) is not list or len(data['words']) > 20_000):
        raise ReviewError('invalid')
    profile, words = data['profile'], data['words']
    for name, maximum in (('language', 128), ('provenance', 4096)):
        value = profile[name]
        if type(value) is not str or not value.strip() or len(value) > maximum or '\0' in value:
            raise ReviewError('invalid')
    if any(type(word) is not str or not word or len(word) > 128 or '\0' in word for word in words):
        raise ReviewError('invalid')
    if len(set(words)) != len(words):
        raise ReviewError('invalid')
    # The parent owns this directory and keeps its request and copied files
    # until the current process owner confirms quiescence.
    # ASCII dictionary basenames avoid native narrow-path encoding problems.
    staging = data['staging']
    root = Path.cwd()
    identity = root.lstat()
    if (type(staging) is not dict or set(staging) != {'path', 'device', 'inode'}
            or str(root) != staging['path'] or not root.name.startswith('resonance-hunspell-')
            or type(staging['device']) is not int or type(staging['inode']) is not int
            or (identity.st_dev, identity.st_ino) != (staging['device'], staging['inode'])
            or not stat.S_ISDIR(identity.st_mode) or getattr(identity, 'st_file_attributes', 0) & 0x400
            or {path.name for path in root.iterdir()} != {'request.json'}):
        raise ReviewError('invalid')
    native = root / ('hunspell.dll' if os.name == 'nt' else 'hunspell.so')
    stage(profile['library'], native, 64 * 1024 * 1024)
    stage(profile['affix'], root / 'dictionary.aff', 16 * 1024 * 1024)
    stage(profile['dictionary'], root / 'dictionary.dic', 128 * 1024 * 1024)
    declared_encoding = dictionary_encoding(root)
    library = handle = None
    try:
        library = bind(native)
        handle = library.Hunspell_create(b'dictionary.aff', b'dictionary.dic')
        if not handle:
            raise ReviewError('dictionary')
        try:
            encoding = library.Hunspell_get_dic_encoding(handle).decode('ascii')
            if len(encoding) > 128:
                raise ValueError('encoding')
            if codecs.lookup(encoding).name != declared_encoding:
                raise ValueError('encoding')
        except (AttributeError, UnicodeError, LookupError, ValueError) as exc:
            raise ReviewError('dictionary') from exc
        issues, size = [], 0
        for index, word in enumerate(words):
            try:
                encoded = word.encode(encoding)
            except UnicodeEncodeError:
                reason, suggestions = 'unencodable', []
            else:
                if library.Hunspell_spell(handle, encoded):
                    continue
                reason, suggestions = 'not_in_dictionary', []
                entries = ctypes.POINTER(ctypes.c_char_p)()
                count = library.Hunspell_suggest(handle, ctypes.byref(entries), encoded)
                try:
                    if not 0 <= count <= 10_000 or (count and not entries):
                        raise ReviewError('invalid')
                    for item in range(min(count, 10)):
                        encoded_suggestion = entries[item]
                        if encoded_suggestion is None or len(encoded_suggestion) > 1024:
                            raise ReviewError('invalid')
                        suggestion = encoded_suggestion.decode(encoding)
                        if not suggestion.strip() or len(suggestion) > 128 or '\0' in suggestion:
                            raise ReviewError('invalid')
                        if suggestion not in suggestions:
                            suggestions.append(suggestion)
                finally:
                    if entries:
                        library.Hunspell_free_list(handle, ctypes.byref(entries), count)
            row = {'index': index, 'reason': reason, 'suggestions': suggestions}
            size += len(json.dumps(row, ensure_ascii=False).encode('utf-8'))
            if len(issues) >= 500 or size > 1_500_000:
                raise ReviewError('budget')
            issues.append(row)
        return {'encoding': encoding, 'checked': len(words), 'issues': issues,
                'request_sha256': sha256(raw).hexdigest()}
    finally:
        if handle:
            library.Hunspell_destroy(handle)
        # OS process teardown unloads the library before parent-owned cleanup.


def read_request():
    if (len(sys.argv) != 2 or len(sys.argv[1]) != 64
            or any(char not in '0123456789abcdef' for char in sys.argv[1])):
        raise ReviewError('invalid')
    path, before = regular(str(Path.cwd() / 'request.json'))
    if not 0 < before.st_size <= 8_000_000:
        raise ReviewError('budget')
    with path.open('rb') as stream:
        if stamp(os.fstat(stream.fileno())) != stamp(before):
            raise ReviewError('changed')
        raw = stream.read(before.st_size + 1)
        if stamp(os.fstat(stream.fileno())) != stamp(before):
            raise ReviewError('changed')
    if (len(raw) != before.st_size or stamp(regular(str(path))[1]) != stamp(before)
            or sha256(raw).hexdigest() != sys.argv[1]):
        raise ReviewError('changed')
    return raw


def main():
    try:
        result = review(read_request())
    except ReviewError as exc:
        result = {'error': str(exc)}
    except (OSError, ValueError, TypeError, UnicodeError, KeyError, RecursionError):
        result = {'error': 'invalid'}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))


if __name__ == '__main__':
    main()
