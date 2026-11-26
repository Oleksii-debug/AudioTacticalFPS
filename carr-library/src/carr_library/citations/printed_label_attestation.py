"""Require independent printed-label approval in addition to text attestation."""
from .anchors import Identity, LabelCorrection, Locator


def printed_label_guard(workspace, attest_text, lookup_label):
    """Compose with historical_loader's existing attestation callback.

    lookup_label(identity, physical_index) must return the currently authorized
    historical LabelCorrection or None, with authority/revocation checked by
    the trusted caller. PDF label metadata and DB flags are not approval.
    No label is rewritten: a failed verified label blocks the whole citation.
    """
    if not isinstance(workspace, str) or not workspace.strip() or not callable(attest_text) or not callable(lookup_label):
        raise ValueError('authorized workspace and attestors required')

    def attest(identity, locator, raw_sha256):
        if not isinstance(identity, Identity) or identity.workspace != workspace or not isinstance(locator, Locator):
            return False
        try:
            if locator.verified:
                approval = lookup_label(identity, locator.index)
                if (type(approval) is not LabelCorrection or approval.identity != identity
                        or approval.replacement != locator):
                    return False
            return attest_text(identity, locator, raw_sha256) is True
        except (OSError, LookupError, ValueError, RuntimeError):
            return False

    return attest
