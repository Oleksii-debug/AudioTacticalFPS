"""Explicit schema preparation uses fixed URLs and verifies bytes before publishing."""
from hashlib import sha256
import importlib.util
from io import BytesIO
import json
from pathlib import Path

import pytest


_spec = importlib.util.spec_from_file_location('prepare_daisy_schemas_under_test',
    Path(__file__).resolve().parents[2] / 'scripts' / 'prepare_daisy_schemas.py')
prepare = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prepare)


class Response(BytesIO):
    def __init__(self, raw, url, *, headers=None, status=200):
        super().__init__(raw)
        self.url, self.status = url, status
        self.headers = headers or {}
        self.reads = 0

    def geturl(self):
        return self.url

    def read1(self, count):
        self.reads += 1
        return super().read1(count)


class Opener:
    def __init__(self, payloads):
        self.payloads, self.calls = payloads, []
        self.mutate = lambda response: response

    def open(self, request, *, timeout):
        assert 0 < timeout <= 10
        assert request.get_header('Accept-encoding') == 'identity'
        self.calls.append(request.full_url)
        raw = self.payloads[request.full_url]
        return self.mutate(Response(raw, request.full_url,
                                   headers={'Content-Length': str(len(raw))}))


@pytest.fixture
def case(tmp_path, monkeypatch):
    payloads = {url: ('Reviewed synthetic schema: ' + name).encode('ascii')
                for name, url in prepare.URLS.items()}
    value = {'schema': 1, 'purpose': 'Isolated transport contract fixture only.',
             'files': {name: {'url': url, 'size': len(payloads[url]),
                             'sha256': sha256(payloads[url]).hexdigest()}
                       for name, url in prepare.URLS.items()}}
    manifest = tmp_path / 'pins.json'
    manifest.write_text(json.dumps(value), encoding='utf-8')
    monkeypatch.setattr(prepare, 'PIN_FILE', manifest)
    return tmp_path / 'schemas', manifest, value, Opener(payloads)


def test_all_five_pinned_bytes_are_created_and_receipted(case):
    target, manifest, value, opener = case
    receipt = prepare.prepare(target, opener=opener)
    assert receipt['status'] == 'PASS' and receipt['file_count'] == 5
    assert receipt['manifest_sha256'] == sha256(manifest.read_bytes()).hexdigest()
    assert set(path.name for path in target.iterdir()) == set(prepare.URLS)
    for name, row in value['files'].items():
        assert (target / name).read_bytes() == opener.payloads[row['url']]
    assert opener.calls == [row['url'] for row in value['files'].values()]


@pytest.mark.parametrize('damage', ['different-bytes', 'truncated', 'oversized'])
def test_invalid_download_never_creates_a_partial_destination(case, damage):
    target, _, _, opener = case
    original = opener.mutate
    def mutate(response):
        raw = response.getvalue()
        changed = (b'!' + raw[1:] if damage == 'different-bytes' else
                   raw[:-1] if damage == 'truncated' else raw + b'!')
        return original(Response(changed, response.url, headers=response.headers))
    opener.mutate = mutate
    with pytest.raises(ValueError, match='size/SHA256'):
        prepare.prepare(target, opener=opener)
    assert not target.exists() and len(opener.calls) == 1


@pytest.mark.parametrize('damage', ['redirected-url', 'encoding', 'status', 'content-length'])
def test_unapproved_response_fails_before_body_read(case, damage):
    target, _, _, opener = case
    observed = []
    def mutate(response):
        if damage == 'redirected-url':
            response.url = 'https://unapproved.invalid/schema'
        elif damage == 'encoding':
            response.headers['Content-Encoding'] = 'gzip'
        elif damage == 'status':
            response.status = 206
        else:
            response.headers['Content-Length'] = '99999'
        observed.append(response)
        return response
    opener.mutate = mutate
    with pytest.raises(ValueError):
        prepare.prepare(target, opener=opener)
    assert len(observed) == 1 and observed[0].reads == 0 and not target.exists()


@pytest.mark.parametrize('damage', ['url', 'hash', 'boolean-size', 'missing-file'])
def test_unreviewed_manifest_fails_before_network(case, damage):
    target, manifest, value, opener = case
    row = value['files']['SMIL10.dtd']
    if damage == 'url':
        row['url'] = 'https://unapproved.invalid/SMIL10.dtd'
    elif damage == 'hash':
        row['sha256'] = 'a' * 65
    elif damage == 'boolean-size':
        row['size'] = True
    else:
        value['files'].pop('SMIL10.dtd')
    manifest.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError):
        prepare.prepare(target, opener=opener)
    assert not opener.calls and not target.exists()


def test_duplicate_manifest_field_is_not_silently_accepted(case):
    target, manifest, _, opener = case
    raw = manifest.read_text(encoding='utf-8').replace('"schema": 1', '"schema": 1, "schema": 1')
    manifest.write_text(raw, encoding='utf-8')
    with pytest.raises(ValueError, match='Duplicate'):
        prepare.prepare(target, opener=opener)
    assert not opener.calls and not target.exists()


def test_existing_destination_is_never_read_or_replaced(case):
    target, _, _, opener = case
    target.mkdir()
    original = target / 'unrelated.txt'; original.write_bytes(b'Unrelated original bytes')
    with pytest.raises(FileExistsError, match='not be overwritten'):
        prepare.prepare(target, opener=opener)
    assert not opener.calls
    assert original.read_bytes() == b'Unrelated original bytes'
    assert list(target.iterdir()) == [original]


def test_redirect_handler_never_follows_even_an_official_redirect():
    with pytest.raises(ValueError, match='redirect'):
        prepare._NoRedirect().redirect_request(None, None, 302, 'Found', {},
                                               prepare.URLS['SMIL10.dtd'])


def test_slow_body_still_observes_monotonic_deadline_before_publication(case):
    target, _, _, opener = case
    now = [0]
    def mutate(response):
        read = response.read1
        def slow(count):
            now[0] = prepare.TIMEOUT + 1
            return read(count)
        response.read1 = slow
        return response
    opener.mutate = mutate
    with pytest.raises(TimeoutError, match='deadline'):
        prepare.prepare(target, opener=opener, clock=lambda: now[0])
    assert not target.exists() and len(opener.calls) == 1


def test_failed_prepare_has_an_explicit_failure_receipt_without_success_claim(tmp_path, monkeypatch):
    target, report = tmp_path / 'schemas', tmp_path / 'receipt.json'
    def fail(_):
        raise ValueError('Synthetic exact-byte verification failed.')
    monkeypatch.setattr(prepare, 'prepare', fail)
    assert prepare.main(['--destination', str(target), '--report', str(report)]) == 1
    value = json.loads(report.read_bytes())
    assert value['status'] == 'FAIL' and 'verification failed' in value['error']
    assert not target.exists()


_acceptance_spec = importlib.util.spec_from_file_location('daisy_schema_acceptance_under_test',
    Path(__file__).resolve().parents[2] / 'scripts' / 'daisy_schema_acceptance.py')
acceptance = importlib.util.module_from_spec(_acceptance_spec)
_acceptance_spec.loader.exec_module(acceptance)


@pytest.mark.parametrize('short_file_uri', [False, True])
def test_real_lxml_reads_only_the_reviewed_local_schema_uri(tmp_path, short_file_uri):
    from lxml import etree
    folder = tmp_path / 'selected schemas'; folder.mkdir()
    schema = folder / 'xhtml-special.ent'
    schema.write_bytes(b'<!ELEMENT root EMPTY>')
    uri = schema.as_uri()
    if short_file_uri:
        uri = uri.replace('file:///', 'file:/', 1)
    parser = etree.XMLParser(load_dtd=True, dtd_validation=True, no_network=True,
                             resolve_entities=False, huge_tree=False)
    parser.resolvers.add(acceptance._schema_resolver({schema.name: schema}, etree))
    document = etree.fromstring(('<!DOCTYPE root SYSTEM "' + uri + '"><root/>').encode(), parser)
    assert document.tag == 'root'


@pytest.mark.parametrize('unapproved', [
    'file://other-host/xhtml-special.ent',
    'https://unapproved.invalid/xhtml-special.ent',
    'http://www.w3.org/TR/xhtml1/DTD/xhtml-special.ent?different=1',
    'file:/unreviewed/xhtml-special.ent',
])
def test_schema_resolver_does_not_admit_matching_basenames_or_uri_authorities(tmp_path, unapproved):
    from lxml import etree
    schema = tmp_path / 'xhtml-special.ent'
    schema.write_bytes(b'<!ELEMENT root EMPTY>')
    resolver = acceptance._schema_resolver({schema.name: schema}, etree)
    with pytest.raises(ValueError, match='unapproved resource'):
        resolver.resolve(unapproved, None, None)
