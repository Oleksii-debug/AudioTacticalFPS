"""Current native callback policy; these cases do not claim Windows device proof."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest

from carr_library import providers
from carr_library.providers import (ProfileStore, ProviderProfile, GOOGLE_TRANSLATION_ENDPOINT,
    GOOGLE_TRANSLATION_PROVIDERS, TRANSLATION_PROVIDERS, LOCAL_TRANSLATION_PROVIDER, google_v3_parent)
from carr_library.ui import api_connections, translation_dialog
from carr_library.ui.translation_dialog import TranslationController

PARENT = 'projects/123456789/locations/us-central1'
ENDPOINT = 'https://translation.googleapis.com/v3/' + PARENT
GLOSSARY = PARENT + '/glossaries/terms'
TOKEN = 'SYNTHETIC_NATIVE_FORM_TOKEN'


def profile():
    return ProviderProfile('advanced', 'Synthetic IAM account', 'google-translation-v3',
        ENDPOINT, 'general/nmt', ('translation',), credential_ref='url-api-' + 'a' * 32)


class Service:
    def __init__(self, root):
        self.root, self.calls = root, []
    def preview(self, profile, text, source, target, *, rate_per_million=None, glossary=''):
        self.calls.append(('preview', glossary))
        return {'estimated_cost': None, 'characters': len(text), 'glossary': glossary}
    def submit(self, profile, text, source, target, *, approval, title, glossary=''):
        self.calls.append(('submit', glossary, approval))
        return approval.job_id


def test_controller_binds_glossary_to_preview_approval_and_submission(tmp_path):
    service = Service(tmp_path)
    controller = TranslationController(tmp_path, service=service)
    selected = profile()
    values = selected, 'Exact source', 'en', 'uk', '1'
    controller.preview(*values, glossary=GLOSSARY)
    with pytest.raises(PermissionError):
        controller.submit(*values, approved=True, glossary=GLOSSARY + '-other')
    assert service.calls == [('preview', GLOSSARY)]
    identity = controller.submit(*values, approved=True, glossary=GLOSSARY)
    assert service.calls[-1][1] == GLOSSARY and service.calls[-1][2].job_id == identity


def _configure_method(store):
    source = Path(api_connections.__file__)
    tree = ast.parse(source.read_text(encoding='utf-8'))
    show = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'show_api_connections')
    dialog = next(node for node in show.body if isinstance(node, ast.ClassDef) and node.name == 'Dialog')
    configure = next(node for node in dialog.body if isinstance(node, ast.FunctionDef) and node.name == 'configure')
    namespace = {'ProviderProfile': ProviderProfile, 'uuid': uuid, 'google_v3_parent': google_v3_parent,
                 'GOOGLE_TRANSLATION_ENDPOINT': GOOGLE_TRANSLATION_ENDPOINT,
                 'GOOGLE_TRANSLATION_PROVIDERS': GOOGLE_TRANSLATION_PROVIDERS,
                 'TRANSLATION_PROVIDERS': TRANSLATION_PROVIDERS,
                 'LOCAL_TRANSLATION_PROVIDER': LOCAL_TRANSLATION_PROVIDER,
                 'controller': SimpleNamespace(store=store)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[configure], type_ignores=[])), str(source), 'exec'), namespace)
    return namespace['configure']


@pytest.mark.parametrize('decision', ['new-profile', 'replace-account', 'renew-same', 'cancel-expiry', 'cancel-renewal'])
def test_native_profile_form_keeps_token_only_in_vault_and_renewal_explicit(tmp_path, monkeypatch, decision):
    monkeypatch.setattr(providers, 'time', lambda: 1_800_000_000.0)
    class Vault:
        def __init__(self):
            self.values = {}
        def put(self, reference, binding, value):
            self.values[reference, binding] = value
        def get(self, *args):
            pytest.fail('Opening/configuring a profile must not read its saved token.')
        def delete(self, reference, binding):
            self.values.pop((reference, binding), None)
    vault = Vault()
    store = ProfileStore(tmp_path / 'api-connections.json', vault)
    previous = None
    if decision != 'new-profile':
        previous = store.save_google_token(profile(), TOKEN + '_OLD', 1_800_001_800.0)
    before = store.path.read_bytes() if store.path.exists() else None
    old_values = dict(vault.values)
    prompts, choices = [], []
    def prompt(title, label, value='', **options):
        prompts.append((title, label, options))
        return {'Advanced v3 project': '123456789', 'Advanced v3 location': 'us-central1',
                'Connection profile': 'Synthetic IAM account', 'OAuth access token': TOKEN,
                'OAuth token expiry': None if decision == 'cancel-expiry' else '2027-01-15T08:30:00Z'}[title]
    # Use an explicitly zoned timestamp derived from the fixed clock.
    from datetime import datetime, timezone
    expiry = datetime.fromtimestamp(1_800_001_800.0, timezone.utc).isoformat()
    original_prompt = prompt
    def bounded_prompt(title, label, value='', **options):
        answer = original_prompt(title, label, value, **options)
        return expiry if title == 'OAuth token expiry' and answer is not None else answer
    def choose(title, items):
        choices.append((title, tuple(items)))
        if title == 'Provider adapter':
            return 3
        assert title == 'OAuth credential update'
        return None if decision == 'cancel-renewal' else int(decision == 'renew-same')
    dialog = SimpleNamespace(prompt=bounded_prompt, choose=choose, refresh=Mock(), status=Mock())
    _configure_method(store)(dialog, previous)
    assert next(options for title, _, options in prompts if title == 'OAuth access token')['password'] is True
    assert not store.path.exists() or TOKEN not in store.path.read_text(encoding='utf-8')
    if decision.startswith('cancel'):
        assert store.path.read_bytes() == before and vault.values == old_values
        dialog.refresh.assert_not_called()
        return
    saved = store.profiles()[0]
    assert saved.endpoint == ENDPOINT and saved.model == 'general/nmt' and saved.provider == 'google-translation-v3'
    assert any(TOKEN in value for value in vault.values.values())
    dialog.refresh.assert_called_once()
    if previous is not None:
        assert (saved.credential_ref == previous.credential_ref) is (decision == 'renew-same')
        assert len(choices) == 2


def _run_callback():
    source = Path(translation_dialog.__file__)
    tree = ast.parse(source.read_text(encoding='utf-8'))
    show = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'show_translation')
    run = next(node for node in show.body if isinstance(node, ast.FunctionDef) and node.name == 'run')
    factory = ast.parse('''
def factory(controller, values, job_title, rate, glossary, wx, begin, report, controls):
    current_job = None
    return run
''').body[0]
    factory.body.insert(1, run)
    from decimal import Decimal
    namespace = {'Decimal': Decimal}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[])), str(source), 'exec'), namespace)
    return namespace['factory']


@pytest.mark.parametrize('change', ['text', 'glossary', 'account', 'library'])
def test_native_approval_does_not_launch_after_modal_selection_changes(tmp_path, change):
    selected = profile()
    service = Service(tmp_path)
    controller = TranslationController(tmp_path, service=service)
    current = [selected, 'Original source', 'en', 'uk', '1']
    title, rate, glossary = Mock(), Mock(), Mock()
    title.GetValue.return_value, rate.GetValue.return_value, glossary.GetValue.return_value = 'Title', '', GLOSSARY
    controller.preview(*current, title='Title', glossary=GLOSSARY)
    moved = [False]
    def values():
        if moved[0]:
            raise ValueError('Library changed.')
        return tuple(current)
    def approve(*args, **kwargs):
        if change == 'text':
            current[1] += ' changed'
        elif change == 'glossary':
            glossary.GetValue.return_value += '-other'
        elif change == 'account':
            from dataclasses import replace
            current[0] = replace(selected, name='Different account')
        else:
            moved[0] = True
        return 1
    wx = SimpleNamespace(MessageBox=approve, YES=1, YES_NO=2, NO_DEFAULT=4, ICON_QUESTION=8)
    begin, report, controls = Mock(), Mock(), Mock()
    # The changed selection must stop before any worker or saved job is made.
    _run_callback()(controller, values, title, rate, glossary, wx, begin, report, controls)(None)
    assert all(call[0] == 'preview' for call in service.calls)
    begin.assert_not_called()
    assert report.called
