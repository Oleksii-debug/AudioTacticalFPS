"""Evidence limits and selection in the read-only catalog; native checks are separate."""
from carr_library.language_catalog import CAPABILITIES, MAX_QUERY, load_catalog
from carr_library.ui.language_catalog_dialog import LanguageCatalogActions, entry_text


class Control:
    def __init__(self, value=''):
        self.value, self.items, self.selection = value, [], -1
    def GetValue(self): return self.value
    def SetValue(self, value): self.value = value
    def GetSelection(self): return self.selection
    def SetSelection(self, value): self.selection = value
    def SetItems(self, value): self.items, self.selection = list(value), -1


def actions():
    result = LanguageCatalogActions()
    result.catalog, result.rows = load_catalog(), ()
    result.filter, result.languages, result.details, result.status = (Control() for _ in range(4))
    result.refresh()
    return result


def test_partial_group_is_not_presented_as_supported_voice():
    view = actions()
    view.filter.SetValue('kab')
    view.refresh()
    assert [row.id for row in view.rows] == ['tamazight']
    assert 'partial-variant-resources-present-unverified' in view.languages.items[0]
    assert 'unresolved_group_standard' in view.details.value
    assert 'No voice is selected or substituted' in view.details.value
    assert 'not establish current local installation' in view.details.value
    assert all(name.replace('_', ' ') + ': not assessed' in view.details.value for name in CAPABILITIES)
    assert 'transcription is deferred' in view.details.value


def test_filter_preserves_same_selected_identity_and_changes_empty_state():
    view = actions()
    index = next(i for i, row in enumerate(view.rows) if row.id == 'uk')
    view.languages.SetSelection(index)
    view.selection_changed()
    view.refresh()
    assert view.rows[view.languages.GetSelection()].id == 'uk'
    view.filter.SetValue('no-selected-language-has-this-name')
    view.refresh()
    assert not view.rows and view.languages.selection == -1
    assert 'No selected languages match' in view.details.value
    assert '0 matching selected languages out of 135' in view.status.value


def test_invalid_query_clears_old_selection_without_changing_catalog():
    view = actions()
    original = view.catalog.entries
    view.filter.SetValue('x' * (MAX_QUERY + 1))
    view.refresh()
    assert not view.rows and not view.languages.items
    assert 'at most' in view.details.value
    assert view.catalog.entries is original


def test_every_selected_entry_exposes_geography_and_unassessed_capabilities():
    for entry in load_catalog().entries:
        text = entry_text(entry)
        assert entry.id in text and entry.name_uk in text
        assert all(place.region in text and all(country in text for country in place.countries)
                   for place in entry.geography)
        assert text.count(': not assessed') == len(CAPABILITIES)
        assert 'Windows/NVDA acceptance' in text
