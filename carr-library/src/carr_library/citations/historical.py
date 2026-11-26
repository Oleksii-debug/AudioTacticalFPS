"""Read-only bridge from sealed SQLite revisions to the existing citation API."""
from .anchors import Identity, Locator, digest


def historical_loader(db, workspace, attest_text):
    """Bind a reader to an authorized workspace and a caller-owned read transaction.

    attest_text(identity, locator, raw_sha256) must consult independent, trusted
    extraction-verification evidence. Storage's verified field is *only* the
    printed label status. It must never be used as extraction verification.
    The caller also authenticates saved metadata and membership separately.
    """
    if not isinstance(workspace, str) or not workspace.strip() or not callable(attest_text):
        raise ValueError('workspace and extraction attestor required')

    def load(identity, locator):
        if not db.in_transaction or db.execute('PRAGMA read_uncommitted').fetchone()[0]:
            raise ValueError('consistent read transaction required')
        if not isinstance(identity, Identity) or not isinstance(locator, Locator) or identity.workspace != workspace:
            raise ValueError('invalid or cross-workspace source')
        # A physical locator is not an ordinal. Logical segments have no physical page.
        rows = db.execute('''SELECT r.workspace,r.edition,r.asset,r.id,
            p.ordinal,p.physical,p.printed,p.verified,p.raw,p.digest
            FROM revision r JOIN sealed s ON s.revision=r.id
            JOIN page p ON p.revision=r.id
            WHERE r.workspace=? AND r.edition=? AND r.asset=? AND r.id=?
            AND ((?='physical_page' AND p.physical=?) OR
                 (?='logical_segment' AND p.physical IS NULL AND p.ordinal=?))''',
            (workspace,identity.edition_id,identity.asset_sha256,identity.revision,
             locator.kind,locator.index,locator.kind,locator.index)).fetchmany(2)
        if len(rows) != 1:
            raise ValueError('missing or ambiguous sealed source')
        w, e, asset, rev, ordinal, physical, printed, verified, raw, stored_digest = rows[0]
        if type(verified) is not int or verified not in (0, 1):
            raise ValueError('invalid label state')
        actual_id = Identity(w,e,asset,rev)
        actual_loc = Locator('physical_page',physical,printed,bool(verified)) if physical is not None else Locator('logical_segment',ordinal,printed,bool(verified))
        if actual_id != identity or actual_loc != locator:
            raise ValueError('source locator drift')
        if digest(raw) != stored_digest:
            raise ValueError('stored text digest mismatch')
        if attest_text(actual_id,actual_loc,stored_digest) is not True:
            raise ValueError('unverified extraction')
        return actual_id,actual_loc,raw,True

    return load
