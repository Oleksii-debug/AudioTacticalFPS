"""Actual isolated FIFO substitution at both selected-file read boundaries."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

import carr_library.conversion.batches as batches


@pytest.mark.parametrize('boundary', ['capture', 'verification'])
def test_replaced_fifo_fails_without_blocking_or_publishing(tmp_path, boundary):
    package = Path(batches.__file__).resolve().parents[1]
    script = r'''
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
import sys
root, expected, base, boundary = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), sys.argv[4]
path = root / 'conversion' / 'batches.py'
assert sha256(path.read_bytes()).hexdigest() == expected
spec = importlib.util.spec_from_file_location('carr_library', root / '__init__.py',
                                            submodule_search_locations=[str(root)])
package = importlib.util.module_from_spec(spec)
sys.modules['carr_library'] = package
spec.loader.exec_module(package)
from carr_library.conversion import batches
from carr_library.conversion.batches import DocumentBatches
assert Path(batches.__file__).resolve() == path
library = base / 'library'; library.mkdir()
source = base / 'source.txt'; source.write_bytes(b'Original regular source')
original = batches.os.open
swapped = False
def opened(selected, flags, *args, **kwargs):
    global swapped
    if Path(selected) == source and not swapped:
        swapped = True
        source.rename(base / 'preserved-original.txt')
        os.mkfifo(source)
    return original(selected, flags, *args, **kwargs)
batches.os.open = opened
try:
    if boundary == 'capture':
        DocumentBatches(library).create([source])
    else:
        batches._file(source, 1024, lambda: False)
except (ValueError, OSError):
    pass
else:
    raise AssertionError('A FIFO became an accepted source.')
assert swapped and (base / 'preserved-original.txt').read_bytes() == b'Original regular source'
assert not list((library / 'state' / 'conversion' / 'batches').glob('*.json')) if boundary == 'capture' else True
'''
    from hashlib import sha256
    result = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c', script,
        str(package), sha256(Path(batches.__file__).read_bytes()).hexdigest(), str(tmp_path), boundary],
        capture_output=True, text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr
