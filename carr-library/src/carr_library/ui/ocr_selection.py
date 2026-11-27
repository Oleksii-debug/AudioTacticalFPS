"""Explicit native selection of installed OCR languages and managed profiles.

Inventory is local. It is not language identification and does not execute OCR.
The trusted runtime's model listing runs off the UI thread with bounded output,
cancellation and a deadline. wx is imported only when showing a dialog.
"""
from dataclasses import replace
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryFile
import time

from ..extract.ocr_languages import model_ids
from ..extract.page_ocr import Config
from ..extract.pinned_tessdata import MODELS, POLICY


PROFILES = (('installed', 'Models installed with the current OCR runtime'),
            ('fast', 'Managed Fast models'), ('best', 'Managed Best models'))
AUXILIARY = frozenset(('osd', 'equ'))
MAX_INVENTORY_BYTES = 128 * 1024


class OcrEngineMissingError(ValueError):
    """No selected/installed engine exists; distinct from a broken runtime."""


def parse_installed_models(data, *, expected_directory=None):
    """Parse a complete Tesseract listing without treating its text as paths."""
    if type(data) is not bytes or not 0 < len(data) <= MAX_INVENTORY_BYTES:
        raise ValueError('The installed OCR model listing exceeds its limit or is empty.')
    try:
        lines = data.decode('utf-8-sig').splitlines()
    except UnicodeError:
        raise ValueError('The installed OCR model listing is not valid UTF-8.') from None
    if not lines:
        raise ValueError('The installed OCR model listing is empty.')
    header = re.fullmatch(r'List of available languages in "([^"\r\n]+)" \(([0-9]+)\):', lines[0])
    if header is None:
        raise ValueError('The OCR runtime returned an unsupported model listing.')
    if expected_directory is not None and (
            os.path.normcase(os.path.abspath(header[1])) !=
            os.path.normcase(os.path.abspath(expected_directory))):
        raise ValueError('The OCR runtime reported a different model directory.')
    count = int(header[2])
    identifiers = tuple(line for line in lines[1:] if line)
    if (count != len(identifiers) or count > 1024 or len(set(identifiers)) != count
            or any(len(identifier) > 128 for identifier in identifiers)):
        raise ValueError('The OCR runtime returned an invalid model inventory.')
    for identifier in identifiers:
        if model_ids(identifier) != (identifier,):
            raise ValueError('The OCR runtime returned an invalid model identifier.')
    return identifiers


def discover_installed_models(*, cancelled=lambda: False, timeout=10.0):
    """Inspect the existing trusted engine, never probe CWD or change its data."""
    from ..extract.ocr_runtime import discover_ocr_runtime
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
        raise ValueError('Invalid OCR inventory timeout.')
    deadline = time.monotonic() + timeout

    def remaining():
        if cancelled():
            raise InterruptedError('OCR model discovery cancelled.')
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError('OCR model discovery timed out. Check the local OCR runtime and retry.')
        return seconds

    remaining()
    # The existing portable-engine contract includes English. System PATH
    # discovery does not require English, so other installed languages work.
    runtime = discover_ocr_runtime('eng', remaining=remaining)
    if runtime is None:
        raise OcrEngineMissingError('No local OCR runtime was found. Install or select the OCR runtime first.')
    with TemporaryFile() as output, TemporaryFile() as errors:
        remaining()
        process = subprocess.Popen(runtime.command([runtime.executable, '--list-langs']),
            stdin=subprocess.DEVNULL, stdout=output, stderr=errors, cwd=runtime.root,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            while process.poll() is None:
                remaining()
                if (os.fstat(output.fileno()).st_size > MAX_INVENTORY_BYTES
                        or os.fstat(errors.fileno()).st_size > MAX_INVENTORY_BYTES):
                    raise ValueError('The OCR runtime exceeded its model-listing output limit.')
                try:
                    process.wait(timeout=min(0.05, remaining()))
                except subprocess.TimeoutExpired:
                    pass
            remaining()
            if (os.fstat(output.fileno()).st_size > MAX_INVENTORY_BYTES
                    or os.fstat(errors.fileno()).st_size > MAX_INVENTORY_BYTES):
                raise ValueError('The OCR runtime exceeded its model-listing output limit.')
            if process.returncode:
                raise ValueError('The OCR runtime could not list its installed models.')
            output.seek(0)
            identifiers = parse_installed_models(output.read(MAX_INVENTORY_BYTES + 1),
                                                expected_directory=runtime.tessdata)
            runtime.assert_unchanged(remaining)
            remaining()
            return identifiers
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)


def selectable_models(profile, identifiers, *, initial=None):
    """Join trusted catalogue labels to exact installed IDs, excluding OSD/equ."""
    from ..extract.model_catalog import models
    if profile not in dict(PROFILES):
        raise ValueError('Choose an OCR model profile.')
    if isinstance(identifiers, (str, bytes)):
        raise ValueError('Invalid installed model inventory.')
    identifiers = tuple(identifiers)
    if (len(set(identifiers)) != len(identifiers) or len(identifiers) > 1024
            or any(model_ids(identifier) != (identifier,) for identifier in identifiers)):
        raise ValueError('Invalid installed model inventory.')
    catalogue = {row.identifier: row for kind in ('fast', 'best') for row in models(kind)}
    chosen_profile = {row.identifier: row for row in models(profile)} if profile != 'installed' else catalogue
    if profile != 'installed' and not set(identifiers) <= set(chosen_profile):
        raise ValueError('Installed models do not match the chosen profile catalogue.')
    rows = []
    for identifier in identifiers:
        model = chosen_profile.get(identifier)
        if identifier.casefold() in AUXILIARY or model is not None and model.kind == 'auxiliary':
            continue
        kind = model.kind if model is not None else 'script' if identifier.startswith('script/') else 'language'
        label = model.label if model is not None else identifier
        rows.append((identifier, f'{label} — {identifier} — {kind}'))
    rows.sort(key=lambda row: (row[1].casefold(), row[0]))
    # Preserve an existing language-priority order when reopening a selection.
    preferred = model_ids((initial or Config()).languages)
    rank = {identifier: index for index, identifier in enumerate(preferred)}
    rows.sort(key=lambda row: rank.get(row[0], len(rank)))
    return tuple(rows)


def selected_config(profile, identifiers, available, *, orientation=False, initial=None):
    """Validate final native choices and preserve existing resource budgets."""
    if profile not in dict(PROFILES):
        raise ValueError('Choose an OCR model profile.')
    if isinstance(identifiers, (str, bytes)) or isinstance(available, (str, bytes)):
        raise ValueError('Select one or more installed text languages or scripts.')
    chosen, available = tuple(identifiers), set(available)
    if (not chosen or len(set(chosen)) != len(chosen) or not set(chosen) <= available
            or any(model_ids(identifier) != (identifier,) for identifier in chosen)
            or any(identifier.casefold() in AUXILIARY for identifier in chosen)):
        raise ValueError('Select one or more installed text languages or scripts.')
    if type(orientation) is not bool or orientation and 'osd' not in available:
        raise ValueError('Install the osd model in this profile to enable orientation detection.')
    previous = initial or Config()
    policy = previous.model_policy
    if policy == POLICY and (profile == 'best' or not set(chosen) <= set(MODELS)):
        policy = 'default'
    return replace(previous, languages='+'.join(chosen), model_profile=profile,
                   orientation=orientation, model_policy=policy)


def choose_model_config(parent, *, initial=None, preset_root=None):
    """Use the shared ordered chooser, including saved presets and both profiles."""
    from .ocr_options import choose_model_config as choose
    if preset_root is None:
        preset_root = getattr(getattr(parent, 'library', None), 'root', None)
    return choose(parent, initial, preset_root=preset_root)
