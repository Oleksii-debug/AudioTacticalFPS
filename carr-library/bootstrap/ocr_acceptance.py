"""Explicit prerequisites for actual OCR acceptance; no downloads or skips.

The Windows job prepares a verified engine outside the checkout. Its CI path is
bound only by these requested tests, never by production runtime discovery.
"""
from hashlib import sha1
import os
from pathlib import Path
import re

from carr_library.extract.model_catalog import models
from carr_library.extract.ocr_runtime import discover_ocr_runtime
from carr_library.extract.ocr_process import run_ocr


def prepared_ocr(monkeypatch, languages=('eng', 'osd')):
    selected = os.environ.get('CARR_CI_OCR_DIR')
    if selected is not None:
        assert selected and Path(selected).is_absolute(), 'CARR_CI_OCR_DIR must identify the prepared absolute bundle.'
        monkeypatch.setenv('CARR_OCR_DIR', selected)
    runtime = discover_ocr_runtime('eng')
    assert runtime is not None, 'Prepare the real local OCR engine before running its required acceptance suite.'
    runtime.assert_unchanged()
    listing = run_ocr(runtime, [runtime.executable, '--list-langs'], remaining=lambda: 15)
    report = listing.stdout.decode('utf-8')
    match = re.search(r'^List of available languages in "([^"\r\n]+)"', report, re.MULTILINE)
    assert match is not None, 'The prepared engine must report its actual installed model directory.'
    source = runtime.tessdata if runtime.tessdata is not None else Path(match[1])
    table = {model.identifier: model for model in models('fast')}
    for language in languages:
        path = source / (language + '.traineddata')
        assert path.is_file(), f'Prepare the official Fast {language} model before running acceptance.'
        raw = path.read_bytes()
        actual = sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
        assert (len(raw), actual) == (table[language].size, table[language].blob_sha1), (
            f'The prepared {language} model must match the reviewed official Fast revision.')
    runtime.assert_unchanged()
    return runtime, source
