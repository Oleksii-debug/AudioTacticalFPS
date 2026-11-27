"""Standalone private Spylls worker. The parent owns admission, deadline and process tree."""
from hashlib import sha256
import json
import importlib.abc
import importlib.util
import re
import stat
import math
import os
from pathlib import Path
import sys
import unicodedata


def _tokens(text, extras, maximum):
    def letter(char):
        return unicodedata.category(char)[0] in 'LM'
    def constituent(char):
        return letter(char) or char in extras
    tokens, index = [], 0
    while index < len(text):
        if not constituent(text[index]):
            index += 1
            continue
        start = index
        index += 1
        while index < len(text):
            char = text[index]
            if constituent(char):
                index += 1
            elif char in "-'’" and index + 1 < len(text) and letter(text[index + 1]):
                index += 1
            else:
                break
        if not any(letter(char) for char in text[start:index]):
            continue
        if index - start > 96:
            raise ValueError('A review token exceeds 96 characters. Select a smaller ordinary-language passage.')
        tokens.append((start, index, text[start:index]))
        if len(tokens) > maximum:
            raise ValueError('Select a smaller passage within the spelling token budget.')
    return tokens


def _pair(stage, request):
    for suffix, limit, max_lines in (('aff', 2 * 1024**2, 50000), ('dic', 16 * 1024**2, 200001)):
        path = stage / ('dictionary.' + suffix)
        if not 0 < path.stat().st_size <= limit:
            raise ValueError('The dictionary exceeds its byte budget.')
        raw = path.read_bytes()
        if sha256(raw).hexdigest() != request[suffix + '_sha256'] or b'\0' in raw:
            raise ValueError('The dictionary snapshot is invalid or changed.')
        lines = raw.splitlines()
        if not lines or len(lines) > max_lines or any(len(line) > 8192 for line in lines):
            raise ValueError('The dictionary exceeds its entry or line budget.')
        if suffix == 'dic':
            header = lines[0].removeprefix(b'\xef\xbb\xbf').strip()
            if not header.isdigit() or len(header) > 6 or not 0 < int(header) <= 200000:
                raise ValueError('The dictionary needs a bounded Hunspell entry count.')
        else:
            # These rare compound features have known upstream coverage limits.
            # Refuse them explicitly instead of silently claiming full Hunspell
            # equivalence for a dictionary whose correctness depends on them.
            unsupported = {b'CHECKCOMPOUNDPATTERN', b'COMPOUNDSYLLABLE', b'SYLLABLENUM', b'COMPOUNDROOT'}
            if any(line.strip().split(None, 1)[0].removeprefix(b'\xef\xbb\xbf') in unsupported for line in lines if line.strip()):
                raise ValueError('This dictionary requires compound directives not supported by the reviewed backend.')



def _backend_sources(backend):
    """Capture all checked code before importing any of the reviewed package."""
    root = Path(backend['root']).resolve(strict=True)
    sources = {}
    for relative, expected in backend['files'].items():
        if (type(relative) is not str
                or re.fullmatch(r'(?:spylls|tests)/(?:[A-Za-z_][A-Za-z_0-9]*/)*[A-Za-z_][A-Za-z_0-9]*\.py', relative) is None):
            raise ValueError('The spelling backend has an invalid source path.')
        selected = root / relative
        before = selected.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or getattr(before, 'st_file_attributes', 0) & 0x400
                or before.st_size > 256 * 1024 or not selected.resolve().is_relative_to(root)):
            raise ValueError('The spelling backend has an invalid source file.')
        with selected.open('rb') as stream:
            raw = stream.read(256 * 1024 + 1)
        if len(raw) != before.st_size or sha256(raw).hexdigest() != expected:
            raise ValueError('The spelling backend changed before worker import.')
        sources[relative] = raw
    return root, sources


class _VerifiedSource(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Import only captured, checksum-approved Python; never ambient bytecode."""
    def __init__(self, root, sources):
        self.root, self.sources = root, sources
        self.modules = {}

    def find_spec(self, fullname, path=None, target=None):
        if fullname != 'spylls' and not fullname.startswith('spylls.'):
            return None
        base = fullname.replace('.', '/')
        for relative, package in ((base + '/__init__.py', True), (base + '.py', False)):
            if relative in self.sources:
                self.modules[fullname] = relative
                selected = self.root / relative
                spec = importlib.util.spec_from_loader(fullname, self,
                    origin=str(selected), is_package=package)
                if package:
                    spec.submodule_search_locations = [str(selected.parent)]
                return spec
        raise ImportError('An unreviewed spelling backend module was requested.')

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        relative = self.modules[module.__name__]
        filename = str(self.root / relative)
        module.__file__, module.__cached__ = filename, None
        exec(compile(self.sources[relative], filename, 'exec'), module.__dict__)


def main():
    if len(sys.argv) != 2:
        raise ValueError('One private spelling request is required.')
    path = Path(sys.argv[1]).resolve(strict=True)
    if path.stat().st_size > 2 * 1024**2:
        raise ValueError('The spelling request is too large.')
    request = json.loads(path.read_text(encoding='utf-8'))
    if type(request) is not dict or type(request.get('schema')) is not int or request['schema'] != 1:
        raise ValueError('Unsupported spelling request.')
    budget, text = request['budget'], request['text']
    if not isinstance(text, str) or not 0 < len(text) <= 200000 or '\0' in text:
        raise ValueError('Invalid spelling text scope.')
    if os.name == 'posix':
        import resource
        memory = budget['memory_bytes']
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_CPU, (max(1, math.ceil(budget['timeout'])),
                                               max(2, math.ceil(budget['timeout']) + 1)))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    stage = path.parent
    def audit(event, args):
        if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system', 'os.posix_spawn'):
            raise PermissionError('The spelling worker does not permit network or child commands.')
        if event in ('os.remove', 'os.rename', 'os.rmdir', 'os.mkdir', 'os.link',
                     'os.symlink', 'os.chmod', 'os.chown', 'os.utime', 'os.truncate'):
            raise PermissionError('The spelling worker does not permit filesystem mutations.')
        if event == 'open' and args:
            mode, flags = args[1] if len(args) > 1 else None, args[2] if len(args) > 2 else 0
            if (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
                    isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)):
                raise PermissionError('The spelling worker is read-only.')
    sys.addaudithook(audit)
    _pair(stage, request)
    backend = request['backend']
    root, sources = _backend_sources(backend)
    # The parent copied only approved source to this private operation. Import
    # the checked in-memory bytes so a later file/pyc replacement cannot execute.
    sys.pycache_prefix = str(stage / 'unused-pycache')
    sys.meta_path.insert(0, _VerifiedSource(root, sources))
    import spylls
    if Path(spylls.__file__).resolve() != root / 'spylls' / '__init__.py':
        raise ValueError('The spelling backend import came from an unexpected location.')
    from spylls.hunspell import Dictionary
    dictionary = Dictionary.from_files(str(stage / 'dictionary'))
    extras = dictionary.aff.WORDCHARS or ''
    if not isinstance(extras, str) or len(extras) > 256 or any(ord(c) < 32 for c in extras):
        raise ValueError('Unsupported dictionary word-character declaration.')
    tokens = _tokens(text, extras, budget['max_tokens'])
    issues, checked = [], 0
    for start, end, word in tokens:
        checked += 1
        if dictionary.lookup(word):
            continue
        suggestions = []
        for suggestion in dictionary.suggest(word):
            if (not isinstance(suggestion, str) or not 0 < len(suggestion) <= 128
                    or any(ord(c) < 32 for c in suggestion)):
                raise ValueError('A dictionary suggestion exceeded its supported text budget.')
            if suggestion not in suggestions:
                suggestions.append(suggestion)
            if len(suggestions) == budget['max_suggestions']:
                break
        issues.append([start, end, word, suggestions])
        if len(issues) == budget['max_issues']:
            break
    result = {'schema': 1, 'text_sha256': sha256(text.encode('utf-8')).hexdigest(),
        'aff_sha256': request['aff_sha256'], 'dic_sha256': request['dic_sha256'],
        'worker_sha256': sha256(Path(__file__).read_bytes()).hexdigest(),
        'backend_version': backend['version'], 'backend_wheel_sha256': backend['wheel_sha256'],
        'issues': issues, 'checked_tokens': checked, 'total_tokens': len(tokens),
        'complete': checked == len(tokens)}
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(encoded) > 512 * 1024:
        raise ValueError('The spelling review exceeded its reply budget.')
    sys.stdout.buffer.write(encoded)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(type(exc).__name__ + ': ' + str(exc)[:1000], file=sys.stderr)
        raise SystemExit(1)
