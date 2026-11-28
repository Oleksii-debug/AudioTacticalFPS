"""Required inventory regressions, independent of optional native OCR fixtures.

The cases reuse the reviewed PR160 traversal regression bodies. The synthetic
bundle fixture comes from the existing portable-runtime suite; no source-root
injection or alternate verifier is introduced.
"""
from pathlib import Path
import runpy

import pytest

from carr_library.extract import ocr_runtime as runtime

_existing = runpy.run_path(str(Path(__file__).with_name('test_ocr_runtime.py')))
bundle = _existing['bundle']


def test_unreadable_directory_rejects_entire_inventory(bundle, monkeypatch):
    hidden = bundle / 'unreadable'
    hidden.mkdir()
    (hidden / 'unlisted.dll').write_bytes(b'unreviewed')
    original = runtime.os.scandir

    def denied(path):
        if Path(path) == hidden:
            raise PermissionError('synthetic enumeration failure')
        return original(path)

    monkeypatch.setattr(runtime.os, 'scandir', denied)
    with pytest.raises(runtime.OcrRuntimeError, match='unavailable or invalid'):
        runtime.verify_ocr_bundle(bundle)


def test_empty_directory_inventory_has_a_bound(bundle, monkeypatch):
    monkeypatch.setattr(runtime, 'MAX_FILES', 4)
    for index in range(4):
        (bundle / f'empty-{index}').mkdir()
    with pytest.raises(runtime.OcrRuntimeError, match='directory limit'):
        runtime.verify_ocr_bundle(bundle)


def test_directory_inventory_checks_budget_between_entries(bundle, monkeypatch):
    # Isolate the inventory stage: budget is checked before accepting each
    # entry, including empty directories, not only once per os.walk yield.
    for index in range(10):
        (bundle / f'empty-{index}').mkdir()
    count = 0

    def remaining():
        nonlocal count
        count += 1
        if count == 4:
            raise TimeoutError('synthetic budget expiry')

    with pytest.raises(TimeoutError, match='budget expiry'):
        runtime._inventory(bundle, remaining)
    assert count == 4
