from contextlib import closing
from hashlib import sha256
import os

import pytest

from carr_library.citations.anchors import Identity, Locator
from carr_library.citations.streamed_text_attestation import streamed_text_attestor
from carr_library.ingest.streamed_ingest import StreamedTextIngest
from carr_library.ingest.streamed_text import TextProfile
from carr_library.storage.revisions import Storage


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'original.txt'
    raw = b'Exact first field\fSecond field remains exact'
    path.write_bytes(raw)
    with closing(Storage(tmp_path / 'library.sqlite')) as storage:
        with closing(StreamedTextIngest(tmp_path / 'imports.sqlite', storage, profile=TextProfile())) as ingest:
            job = ingest.start('scope', 'edition', tmp_path, path.name)
            assert ingest.run('scope', job, tmp_path, path.name)['state'] == 'complete'
        storage.db.execute('BEGIN')
        try:
            yield storage.db, path, Identity('scope', 'edition', sha256(raw).hexdigest(), job)
        finally:
            storage.db.rollback()


def test_one_action_reuses_only_unchanged_native_bound_hash_index(source, monkeypatch):
    from carr_library.citations import streamed_text_attestation as module
    db, path, identity = source
    real, scans = module.inspect_text, []
    def observed(*args, **kwargs):
        scans.append(True)
        return real(*args, **kwargs)
    monkeypatch.setattr(module, 'inspect_text', observed)
    attest = streamed_text_attestor(db, 'scope', lambda _: path)
    assert attest(identity, Locator('logical_segment', 1), sha256(b'Exact first field').hexdigest())
    assert attest(identity, Locator('logical_segment', 2), sha256(b'Second field remains exact').hexdigest())
    assert len(scans) == 1
    # A new action does not inherit the prior action's cache or authority.
    assert streamed_text_attestor(db, 'scope', lambda _: path)(identity, Locator('logical_segment', 1),
                                                              sha256(b'Exact first field').hexdigest())
    assert len(scans) == 2


def test_same_timestamp_change_outside_quote_invalidates_an_already_warm_action(source):
    db, path, identity = source
    attest = streamed_text_attestor(db, 'scope', lambda _: path)
    wanted = sha256(b'Exact first field').hexdigest()
    assert attest(identity, Locator('logical_segment', 1), wanted)
    before = path.stat()
    with path.open('r+b') as output:
        output.seek(-1, os.SEEK_END)
        output.write(b'X')
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert not attest(identity, Locator('logical_segment', 1), wanted)


def test_authorized_scope_and_locator_are_checked_before_any_source_access(source):
    db, path, identity = source
    attest = streamed_text_attestor(db, 'scope', lambda _: pytest.fail('Must not resolve a foreign source'))
    assert not attest(Identity('other', identity.edition_id, identity.asset_sha256, identity.revision),
                      Locator('logical_segment', 1), 'a' * 64)
    assert not attest(identity, Locator('physical_page', 1), 'a' * 64)


def test_attestation_propagates_cancellation_instead_of_returning_false_evidence(source):
    db, path, identity = source
    attest = streamed_text_attestor(db, 'scope', lambda _: path, cancelled=lambda: True)
    with pytest.raises(InterruptedError):
        attest(identity, Locator('logical_segment', 1), 'a' * 64)
