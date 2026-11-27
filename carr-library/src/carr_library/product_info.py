"""Visible product identity; package names and existing data locations stay stable."""
from importlib.metadata import PackageNotFoundError, version

from .citations.csl import ATTRIBUTION

PRODUCT_NAME = 'Resonance'
ORGANIZATION = 'Sceneria Innovative Technologies'
WINDOW_TITLE = PRODUCT_NAME + ' — ' + ORGANIZATION


def about_text():
    try:
        installed_version = version('universal-research-library')
    except PackageNotFoundError:
        installed_version = 'source checkout'
    return (
        f'{PRODUCT_NAME}\nA platform by {ORGANIZATION}\nVersion: {installed_version}\n\n'
        'Local libraries, academic research, accessible reading and audio production.\n'
        'Additional voice, music and transcription engines require their own configured runtimes and models.\n\n'
        'Component notices\n'
        'Third-party components retain their original copyrights and license terms. '
        'Full notices, provenance and corresponding-source information accompany the package. '
        'The Resonance name does not replace those notices.\n\n'
        + ATTRIBUTION + '\n'
    )
