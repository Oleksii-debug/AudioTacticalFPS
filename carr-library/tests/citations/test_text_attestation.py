import hashlib
from dataclasses import replace

import pytest

from carr_library.citations.anchors import Identity, Locator, Metadata, digest, make_anchor, resolve_span
from carr_library.citations.historical import historical_loader
from carr_library.citations.text_attestation import CONFIG, strict_text_attestor
from carr_library.storage.revisions import Page, Storage

RAW = 'Київ — e\u0301\r\n'
DATA = b'\xef\xbb\xbf' + (RAW + '\fsecond').encode('utf-8')
OLD = Identity('A', 'edition', hashlib.sha256(DATA).hexdigest(), 'old')
LOC = Locator('logical_segment', 1)


@pytest.fixture
def store(tmp_path):
    path = tmp_path / 'library.db'
    s = Storage(path)
    s.publish_revision('A', 'edition', 'old', OLD.asset_sha256, CONFIG, [Page(RAW), Page('second')])
    s.publish_revision('A', 'edition', 'new', 'b'*64, CONFIG, [Page('different active revision')])
    s.publish_revision('B', 'edition', 'foreign', OLD.asset_sha256, CONFIG, [Page(RAW), Page('second')])
    s.close()
    s = Storage(path)
    try:
        yield s
    finally:
        s.close()


def test_reopen_historical_exact_quote(store):
    anchor = make_anchor(OLD, LOC, RAW, 0, len(RAW), Metadata('Synthetic author', 'Title'), text_verified=True)
    with store.transaction():
        attest = strict_text_attestor(store.db, 'A', lambda identity: DATA if identity == OLD else None)
        load = historical_loader(store.db, 'A', attest)
        assert resolve_span((anchor,), load) == ((anchor, RAW),)
        assert anchor.quote == RAW
        assert load(OLD, Locator('logical_segment', 2))[2] == 'second'
        assert LOC.display() == 'Segment 1 (unpaginated)'


@pytest.mark.parametrize('asset', [DATA[3:], DATA.replace(b'\r\n', b'\n'), DATA + b'!', None, bytearray(DATA)])
def test_changed_missing_or_nonimmutable_source(store, asset):
    with store.transaction(), pytest.raises(ValueError, match='unverified extraction'):
        historical_loader(store.db, 'A', strict_text_attestor(store.db, 'A', lambda _: asset))(OLD, LOC)


def test_missing_original_exception(store):
    def missing(_):
        raise FileNotFoundError('private original')
    with store.transaction(), pytest.raises(ValueError, match='unverified extraction'):
        historical_loader(store.db, 'A', strict_text_attestor(store.db, 'A', missing))(OLD, LOC)


def test_fabricated_raw_and_matching_db_digest(store):
    with store.transaction():
        store.db.execute('DROP TRIGGER page_bu')
        store.db.execute('UPDATE page SET raw=?, digest=? WHERE revision=? AND ordinal=1',
                         ('fabricated', digest('fabricated'), 'old'))
        with pytest.raises(ValueError, match='unverified extraction'):
            historical_loader(store.db, 'A', strict_text_attestor(store.db, 'A', lambda _: DATA))(OLD, LOC)


@pytest.mark.parametrize('identity,locator', [
    (replace(OLD, workspace='B', revision='foreign'), LOC),
    (replace(OLD, edition_id='other'), LOC),
    (replace(OLD, revision='absent'), LOC),
    (OLD, Locator('physical_page', 1, 'iv', True)),
    (OLD, Locator('logical_segment', 1001)),
])
def test_scope_before_asset_access(store, identity, locator):
    def forbidden(_):
        pytest.fail('asset resolver called for invalid scope/locator')
    with store.transaction():
        assert strict_text_attestor(store.db, 'A', forbidden)(identity, locator, digest(RAW)) is False


def test_unsupported_config_and_unsealed(store):
    store.publish_revision('A', 'other', 'pdf', OLD.asset_sha256, 'pdf-or-ocr', [Page(RAW)])
    def forbidden(_):
        pytest.fail('asset resolver called for unsupported or unsealed revision')
    with store.transaction():
        store.db.execute('INSERT INTO revision VALUES(?,?,?,?,?)', ('draft','A','edition',OLD.asset_sha256,CONFIG))
        attest = strict_text_attestor(store.db, 'A', forbidden)
        assert attest(Identity('A','other',OLD.asset_sha256,'pdf'), LOC, digest(RAW)) is False
        assert attest(replace(OLD, revision='draft'), LOC, digest(RAW)) is False


@pytest.mark.parametrize('data,pages', [(b'\xff', [Page('replacement')]), (b'bad\x00', [Page('bad')]),
                                      (b'one\ftwo', [Page('one')]), (b'\f'*1000, [Page('')])])
def test_invalid_decoding_controls_or_segment_count(store, data, pages):
    ident = Identity('A','other',hashlib.sha256(data).hexdigest(),'invalid')
    store.publish_revision('A','other','invalid',ident.asset_sha256,CONFIG,pages)
    with store.transaction():
        assert strict_text_attestor(store.db, 'A', lambda _: data)(ident,LOC,digest(pages[0].raw)) is False


def test_transaction_required(store):
    attest = strict_text_attestor(store.db, 'A', lambda _: DATA)
    assert attest(OLD,LOC,digest(RAW)) is False
    store.db.execute('PRAGMA read_uncommitted=ON')
    with store.transaction():
        assert attest(OLD,LOC,digest(RAW)) is False


def test_original_disk_file_replaced_or_removed(store, tmp_path):
    source = tmp_path / 'оригінал.txt'
    source.write_bytes(DATA)
    with store.transaction():
        load = historical_loader(store.db, 'A', strict_text_attestor(store.db, 'A', lambda _: source.read_bytes()))
        assert load(OLD, LOC)[2] == RAW
        source.write_bytes(DATA + b'!')
        with pytest.raises(ValueError, match='unverified extraction'):
            load(OLD, LOC)
        source.unlink()
        with pytest.raises(ValueError, match='unverified extraction'):
            load(OLD, LOC)
