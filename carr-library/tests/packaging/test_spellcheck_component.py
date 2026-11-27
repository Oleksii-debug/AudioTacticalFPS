"""The installed optional component is the exact reviewed code-only derivative."""
import base64
import csv
from hashlib import sha256
import importlib.metadata
from io import StringIO
import json
from pathlib import Path
import runpy
from zipfile import ZipFile

import pytest

from carr_library.speech import spellcheck


def test_installed_backend_preserves_all_upstream_python_and_has_no_dictionary_payload():
    backend = spellcheck._backend(lambda: False)
    distribution = importlib.metadata.distribution('spylls')
    assert distribution.version == '0.1.7+carr.1'
    assert distribution.metadata['License'] == 'MPL-2.0'
    assert not distribution.requires
    files = tuple(distribution.files)
    assert not any(str(path).endswith(('.aff', '.dic')) for path in files)
    assert not list((Path(backend['root']) / 'spylls').rglob('*.aff'))
    assert not list((Path(backend['root']) / 'spylls').rglob('*.dic'))
    manifest = json.loads(Path(spellcheck.__file__).with_name('spellcheck_backend.json').read_text())
    provenance = json.loads(distribution.read_text('CARR_REPACK.json'))
    assert provenance == manifest['provenance']
    assert len(provenance['unchanged_python_sha256']) == 21 and len(provenance['removed']) == 6
    assert 'Mozilla Public License Version 2.0' in distribution.read_text('LICENSE')
    for name, expected in provenance['unchanged_python_sha256'].items():
        assert sha256(Path(distribution.locate_file(name)).read_bytes()).hexdigest() == expected
    for name, digest, size in csv.reader(StringIO(distribution.read_text('RECORD'))):
        if not digest:
            continue
        data = Path(distribution.locate_file(name)).read_bytes()
        assert len(data) == int(size)
        assert digest == 'sha256=' + base64.urlsafe_b64encode(sha256(data).digest()).rstrip(b'=').decode()


def test_repack_refuses_unreviewed_input_without_creating_an_output(tmp_path):
    helper = runpy.run_path(str(Path(__file__).parents[2] / 'scripts' / 'repack_spellcheck.py'))
    source = tmp_path / helper['UPSTREAM_NAME']
    output = tmp_path / helper['OUTPUT_NAME']
    source.write_bytes(b'not the reviewed wheel')
    with pytest.raises(ValueError, match='size'):
        helper['repack'](source, output)
    assert not output.exists()
    with source.open('wb') as stream:
        stream.truncate(helper['UPSTREAM_SIZE'])
    with pytest.raises(ValueError, match='hash'):
        helper['repack'](source, output)
    assert not output.exists()


def test_source_lock_and_runtime_manifest_pin_the_same_derived_component():
    root = Path(__file__).parents[2]
    helper = runpy.run_path(str(root / 'scripts' / 'repack_spellcheck.py'))
    manifest = json.loads(Path(spellcheck.__file__).with_name('spellcheck_backend.json').read_text())
    assert manifest['provenance']['upstream_sha256'] == helper['UPSTREAM_SHA256']
    assert manifest['provenance']['upstream_size'] == helper['UPSTREAM_SIZE']
    assert manifest['wheel'] == helper['OUTPUT_NAME']
    active = [line for line in (root / 'packaging' / 'spellcheck.lock').read_text().splitlines()
              if line.strip() and not line.startswith('#')]
    assert active == ['spylls==0.1.7+carr.1 --hash=sha256:' + manifest['sha256']]
