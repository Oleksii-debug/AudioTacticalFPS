"""Independent original-byte proof for the versioned streamed TXT adapter."""
from ..audio.jobs import file_identity, regular_file, verified_reader
from ..ingest.pipeline import canonical
from ..ingest.streamed_text import inspect_text, profile_from_config, check_cancelled
from ..native_file_metadata import stream_change_time
from .anchors import Identity, Locator


def streamed_text_attestor(db, workspace, resolve_original, *, cancelled=lambda: False):
    """Use an authorized retained path, never a source path from a document.

    The small hash index lives only inside this caller's read action. Every new
    action hashes the complete original. Reuse in one action additionally pins
    native change time (Windows creation-time stat is insufficient), file
    identity, and link count. A changed source is rescanned and fails its SHA.
    """
    cache = {}

    def attest(identity, locator, raw_sha256):
        check_cancelled(cancelled)
        if (not db.in_transaction or db.execute('PRAGMA read_uncommitted').fetchone()[0]
                or not isinstance(identity, Identity) or identity.workspace != workspace
                or not isinstance(locator, Locator) or locator.kind != 'logical_segment'):
            return False
        row = db.execute('SELECT r.config,(SELECT count(*) FROM page p WHERE p.revision=r.id) '
            'FROM revision r JOIN sealed s ON s.revision=r.id '
            'WHERE r.workspace=? AND r.edition=? AND r.asset=? AND r.id=?',
            (workspace, identity.edition_id, identity.asset_sha256, identity.revision)).fetchone()
        if row is None:
            return False
        try:
            profile = profile_from_config(row[0])
            if profile is None or row[0] != canonical(profile.configuration()):
                return False
            path = resolve_original(identity)
            key = (identity.asset_sha256, canonical(profile.configuration()))
            plan = cache.get(key)
            fresh = False
            if plan is not None:
                before = regular_file(path)
                if before.st_nlink == 1 and file_identity(before) == plan.identity:
                    with verified_reader(path, expected_identity=plan.identity) as stream:
                        fresh = stream_change_time(stream) == plan.change_time
            if not fresh:
                plan = inspect_text(path, profile, expected_digest=identity.asset_sha256, cancelled=cancelled)
                # Ordinary aggregate cache stays at 16,384 identities; one
                # explicitly admitted large profile can hold up to 65,536
                # bounded hashes. It never retains the original text.
                while cache and (len(cache) >= 8 or sum(len(item.fragments) for item in cache.values())
                                  + len(plan.fragments) > 16384):
                    cache.pop(next(iter(cache)))
                cache[key] = plan
            check_cancelled(cancelled)
            return (len(plan.fragments) == row[1] and 1 <= locator.index <= row[1]
                    and plan.fragments[locator.index - 1].sha256 == raw_sha256)
        except InterruptedError:
            raise
        except (OSError, LookupError, ValueError, RuntimeError):
            return False

    return attest
