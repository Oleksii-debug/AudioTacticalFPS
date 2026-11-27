"""Compatibility API for the single native OCR selector and inventory authority.

The existing ordered selector owns installed, managed and verified external
pack choices, saved presets and missing-engine behavior. There is no second
profile chooser or inventory process here.
"""
from dataclasses import replace

from ..extract.ocr_languages import (MAX_REPORT_BYTES, discover_languages,
    model_ids, parse_language_report)
from ..extract.page_ocr import Config
from ..extract.pinned_tessdata import MODELS, POLICY

PROFILES = (('installed', 'Models installed with the current OCR runtime'),
            ('fast', 'Managed Fast models'), ('best', 'Managed Best models'))
AUXILIARY = frozenset(('osd', 'equ'))
MAX_INVENTORY_BYTES = MAX_REPORT_BYTES


class OcrEngineMissingError(ValueError):
    """Missing installed engine, distinct from a broken selected runtime."""


def parse_installed_models(data, *, expected_directory=None):
    if type(data) is not bytes or not 0 < len(data) <= MAX_INVENTORY_BYTES:
        raise ValueError('The installed OCR model listing exceeds its limit or is empty.')
    return parse_language_report(data.decode('utf-8-sig'), expected_directory=expected_directory)


def discover_installed_models(*, cancelled=lambda: False, timeout=10.0):
    catalog = discover_languages(cancelled=cancelled, timeout=timeout)
    if catalog.integrity == 'unavailable':
        raise OcrEngineMissingError('No local OCR runtime was found. Install or select the runtime first.')
    return catalog.codes


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
                   orientation=orientation, model_policy=policy, model_pack_path=None, model_pack_sha256=None)


def choose_model_config(parent, *, initial=None, preset_root=None):
    """Open the one ordered selector; preserve all Config fields and presets."""
    import wx
    from .ocr_language_dialog import create_ocr_dialog
    if preset_root is None:
        preset_root = getattr(getattr(parent, 'library', None), 'root', None)
    dialog = create_ocr_dialog(parent, initial, preset_root=preset_root)
    try:
        return dialog.result if dialog.ShowModal() == wx.ID_OK else None
    finally:
        dialog.Destroy()
