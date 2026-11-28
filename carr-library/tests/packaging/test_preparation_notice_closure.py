"""Actual installed License-File/RECORD closure between preparation and About."""
from hashlib import sha256
from importlib import resources

from carr_library import component_notices


def test_installed_slovak_notice_is_discoverable_and_exact_in_about_inventory():
    package = resources.files('carr_library.reader._vendor_num2words')
    expected = package.joinpath('COPYING').read_bytes()
    catalogue = component_notices.catalogue()
    components = [item for item in catalogue.components
                  if item.name.casefold().replace('_', '-') == 'universal-research-library']
    assert len(components) == 1
    selected = [notice for notice in component_notices.notices(components[0])
                if notice.relative.endswith('/reader/_vendor_num2words/COPYING')]
    assert selected, 'The actual installed CARR metadata must expose its vendored upstream notice.'
    for notice in selected:
        assert notice.algorithm == 'sha256'
        shown = component_notices.read_notice(notice)
        assert 'Recorded file hash matched.' in shown
        assert '\nSHA-256: ' + sha256(expected).hexdigest() + '\n\n' in shown
        assert shown.endswith('\n\n' + expected.decode('utf-8-sig'))
