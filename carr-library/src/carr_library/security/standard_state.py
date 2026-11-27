"""Bounded handoff state shared by the existing standard installer and launcher.

This receipt is coordination, never publisher authority. Only the TUF client
creates approval; a normal launch merely refuses an uncommitted generation.
"""
from __future__ import annotations

import configparser
from hashlib import sha256
import os
from pathlib import Path
import re
import tempfile

from ..audio.jobs import publish_noreplace
from ._files import TrustError, atomic_write, directory, json_bytes, read_bytes, sync_directory

PROTOCOL = 'resonance-standard-update/1'
PENDING = 'standard-update-pending.ini'
PRIOR = 'standard-update-prior.json'
REJECTED = 'standard-update-rejected.json'
MAX_STATE = 32 * 1024**2
_FIELDS = frozenset({'schema', 'transaction', 'program', 'previous_generation',
    'previous_source', 'previous_manifest', 'previous_python_sha256', 'candidate_source',
    'candidate_manifest', 'candidate_setup', 'candidate_generation', 'prior_sha256'})
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_SOURCE = re.compile(r'[0-9a-f]{40}\Z')
_TOKEN = re.compile(r'[0-9a-f]{32}\Z')
_GENERATION = re.compile(r'versions/([0-9a-f]{12})-([1-9][0-9]{0,5})\Z')


def generation_name(value, source=None):
    if type(value) is not str or not _GENERATION.fullmatch(value):
        raise TrustError('Unsupported standard installation generation.')
    if source is not None and value.split('/')[1].split('-')[0] != source[:12]:
        raise TrustError('The generation does not match its recorded source.')
    return value


def validate_pending(value, root):
    root = directory(root)
    if type(value) is not dict or set(value) != _FIELDS or value['schema'] != PROTOCOL:
        raise TrustError('Unsupported pending standard update state.')
    if any(type(item) is not str or len(item) > 4096 for item in value.values()):
        raise TrustError('Invalid pending update field type or size.')
    if value['program'] != str(root) or not _TOKEN.fullmatch(value['transaction']):
        raise TrustError('The pending update belongs to another program or transaction.')
    for name in ('previous_source', 'candidate_source'):
        if type(value[name]) is not str or not _SOURCE.fullmatch(value[name]):
            raise TrustError('Invalid pending update source identity.')
    if value['previous_source'] == value['candidate_source']:
        raise TrustError('A same-source repair is not a signed application upgrade.')
    for name in ('previous_manifest', 'previous_python_sha256', 'candidate_manifest',
                 'candidate_setup', 'prior_sha256'):
        if type(value[name]) is not str or not _HEX.fullmatch(value[name]):
            raise TrustError('Invalid pending update checksum.')
    generation_name(value['previous_generation'], value['previous_source'])
    if value['candidate_generation']:
        generation_name(value['candidate_generation'], value['candidate_source'])
        if value['candidate_generation'] == value['previous_generation']:
            raise TrustError('An upgrade must keep the previous generation.')
    return dict(value)


def pending_bytes(value, root):
    value = validate_pending(value, root)
    for item in value.values():
        if type(item) is not str or any(ord(char) < 32 for char in item):
            raise TrustError('Pending update fields must be bounded single-line text.')
    return ('[update]\n' + ''.join(name + '=' + value[name] + '\n'
            for name in sorted(value))).encode('utf-8')


def read_pending(root):
    root = directory(root)
    path = root / PENDING
    if not os.path.lexists(path):
        return None
    raw = read_bytes(path, 16384)
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(raw.decode('utf-8'))
    except (configparser.Error, UnicodeError) as error:
        raise TrustError('Invalid pending standard update state.') from error
    if parser.defaults() or parser.sections() != ['update']:
        raise TrustError('Invalid pending update section.')
    value = validate_pending(dict(parser['update']), root)
    if pending_bytes(value, root) != raw:
        raise TrustError('The pending update state is not canonical.')
    return value


def publish_new(path, raw):
    path = Path(path)
    parent = directory(path.parent)
    descriptor, name = tempfile.mkstemp(prefix='.standard-update-', dir=parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        publish_noreplace(Path(name), path)
    finally:
        if os.path.lexists(name):
            Path(name).unlink()


def begin_pending(root, value, prior):
    root = directory(root)
    raw = json_bytes(prior)
    if len(raw) > MAX_STATE or sha256(raw).hexdigest() != value['prior_sha256']:
        raise TrustError('The exact previous activation snapshot is required.')
    if os.path.lexists(root / PENDING) or os.path.lexists(root / PRIOR):
        raise TrustError('Recover the existing standard update before preparing another.')
    # Publish the snapshot first. An interrupted first publication is retained
    # for explicit recovery; it never claims that a new generation can launch.
    publish_new(root / PRIOR, raw)
    publish_new(root / PENDING, pending_bytes(value, root))


def replace_pending(root, before, after):
    root = directory(root)
    if read_pending(root) != before:
        raise TrustError('The pending standard update changed.')
    atomic_write(root / PENDING, pending_bytes(after, root))


def prior_bytes(root, pending):
    raw = read_bytes(directory(root) / PRIOR, MAX_STATE)
    if sha256(raw).hexdigest() != pending['prior_sha256']:
        raise TrustError('The retained previous activation snapshot changed.')
    return raw


def clear_pending(root, expected):
    root = directory(root)
    actual = read_pending(root)
    if actual is not None and actual != expected:
        raise TrustError('The pending standard update changed before completion.')
    if os.path.lexists(root / PRIOR):
        prior_bytes(root, expected)
    elif actual is not None:
        raise TrustError('The previous activation snapshot is unavailable.')
    # Cleanup can resume after a committed pending-file deletion. Only the
    # exact retained snapshot is ever removed, never an unrelated prior file.
    if actual is not None:
        (root / PENDING).unlink()
        sync_directory(root)
    if os.path.lexists(root / PRIOR):
        (root / PRIOR).unlink()
        sync_directory(root)


def check_generation_start(root, environment, arguments, *, bootstrap=False, already_running=False):
    if already_running:
        return
    environment = directory(environment)
    rejected = environment.parent / REJECTED
    if os.path.lexists(rejected) and not already_running:
        # Its contents are diagnostics, never an override or recovery permit.
        read_bytes(rejected, 16384)
        raise TrustError('This retained update generation did not pass activation. Use the registered previous version or Repair.')
    pending = read_pending(root)
    if pending is None or already_running:
        return
    relative = environment.parent.relative_to(directory(root)).as_posix()
    if relative == pending['previous_generation']:
        return
    if (relative == pending['candidate_generation'] and bootstrap is True
            and list(arguments) == ['--self-test']):
        return
    raise TrustError('This generation belongs to a pending update. Keep using the previous version '
                     'and recover the signed update before launching the new application.')
