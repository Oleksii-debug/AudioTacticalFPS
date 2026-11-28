"""Pinned sort-macro compatibility adaptation of the historical CSL processor.

Added 2026-11-27. The three edits are from Dan Stillman's unmerged citeproc-js
PR276, revision adffcfa3189ed21cbdb5272a74a8edbf817ac326, under CPAL-1.0.
The original processor, its license/attribution and all style pins stay intact.
This application adapter is recorded separately in every new document receipt.
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

_PATCH = Path(__file__).resolve().parent / 'vendor' / 'citeproc-sort-macro-cache.json'
_PATCH_SHA256 = 'eb383f9b2dcea13af17cb18582f664befbcbfaef02b07649f9188d4dfdf668ff'
_BASE_SHA256 = '8d80b2d4ad5d249178ddfa4c535bb72bbd1883521a88e9362686558e2f82029d'
_ADAPTED_SHA256 = '48ba59a83c32ca25f8a2c7ca4bf834331aefcc6b8cc66876c1c2bbdd882810ee'
_MAX_PATCH_BYTES = 8192
_IDENTITY_KEYS = (
    'id', 'application_adapter', 'resource_profile', 'upstream_revision',
    'upstream_patch_sha256', 'upstream_source', 'upstream_status', 'license',
    'attribution', 'base_processor_sha256', 'adapted_processor_sha256',
)


def _patch():
    with _PATCH.open('rb') as source:
        raw = source.read(_MAX_PATCH_BYTES + 1)
    if len(raw) > _MAX_PATCH_BYTES or sha256(raw).hexdigest() != _PATCH_SHA256:
        raise ValueError('The citation runtime adaptation failed its integrity check.')
    value = json.loads(raw)
    if (type(value) is not dict or type(value.get('schema')) is not int
            or value['schema'] != 1 or len(value.get('edits', ())) != 3
            or value.get('base_processor_sha256') != _BASE_SHA256
            or value.get('adapted_processor_sha256') != _ADAPTED_SHA256):
        raise ValueError('The citation runtime adaptation has an invalid pinned shape.')
    return value


def _identity(patch):
    # A fresh copy prevents a caller from changing later receipt identities.
    return dict({key: patch[key] for key in _IDENTITY_KEYS}, patch_sha256=_PATCH_SHA256)


def adapter_identity():
    """Return the independently pinned application adapter identity."""
    return _identity(_patch())


def prepare_processor(raw):
    """Verify and apply exactly three pinned edits in memory; never write assets."""
    if type(raw) is not bytes or sha256(raw).hexdigest() != _BASE_SHA256:
        raise ValueError('The original citation processor failed its integrity check.')
    patch = _patch()
    processor = raw.decode('utf-8')
    for edit in patch['edits']:
        before, after = edit['before'], edit['after']
        if (not isinstance(before, str) or not before or not isinstance(after, str)
                or processor.count(before) != 1):
            raise ValueError('The citation runtime adaptation no longer matches its original processor.')
        processor = processor.replace(before, after, 1)
    if sha256(processor.encode('utf-8')).hexdigest() != _ADAPTED_SHA256:
        raise ValueError('The adapted citation processor failed its integrity check.')
    return processor, _identity(patch)


def supported_processor_identity(value, original):
    """Recognize exact historical receipts and the explicit current adaptation."""
    if type(value) is not dict:
        return False
    return value == original or value == dict(original, runtime_adapter=adapter_identity())
