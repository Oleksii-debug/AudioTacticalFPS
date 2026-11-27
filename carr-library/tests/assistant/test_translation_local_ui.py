"""Existing translation controls with local approval and queued-handler boundaries."""
from dataclasses import replace
import json
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.assistant.profiles import ProfileStore
from carr_library.assistant import translation_cli
from carr_library.ui import translation_dialog as ui

from test_translation_local import ready
from test_translation_v3 import transport, translation_handlers


@pytest.fixture
def local_handlers(translation_handlers, ready):
    e = translation_handlers
    store = ProfileStore(e.frame.library.root / 'state/providers')
    store.remove(e.p.id)
    store.save(ready.profile)
    e.p = ready.profile
    e.local = ready
    return e


def fields(e, value='Explicit local selected text.'):
    e.find('Translation provider profile').SetSelection(0)
    e.find('Source language code; auto detects').ChangeValue('en')
    e.find('Target language code').ChangeValue('uk')
    e.find('Advanced v3 glossary resource; blank for none').ChangeValue('')
    e.find('Selected text; originals remain unchanged').ChangeValue(value)
    e.find('Maximum approved budget in USD; no default price').ChangeValue('0')


def prepare(e, value='Explicit local selected text.'):
    fields(e, value)
    e.press('Fetch languages')
    e.complete()


def test_existing_local_handlers_need_two_explicit_approvals_and_send_only_frozen_text(local_handlers):
    e = local_handlers
    before = len(e.local.state['calls'])
    def scenario():
        assert len(e.local.state['calls']) == before
        prepare(e)
        assert len(e.local.state['calls']) == before + 1
        assert 'local LibreTranslate' in e.state['approvals'][0]
        assert e.p.endpoint in e.state['approvals'][0]
        assert 'targets: uk' in e.find('Preview, supported languages and job status').GetValue()
        e.press('Preview')
        assert len(e.local.state['calls']) == before + 1
        assert '"estimated_cost": "0"' in e.find('Preview, supported languages and job status').GetValue()
        e.press('Translate / resume')
        assert len(e.local.state['calls']) == before + 1 and len(e.state['queue']) == 1
        assert 'explicitly selected local' in e.state['approvals'][-1]
        assert e.p.endpoint in e.state['approvals'][-1]
        assert not e.find('Selected text; originals remain unchanged').enabled
        e.complete()
        job, = e.service.list()
        assert job['status'] == 'complete' and job['source_text'] == 'Explicit local selected text.'
        assert job['authorization']['budget'] == '0'
        assert job['profile']['credential_ref'] is None
        assert len(e.local.state['calls']) == before + 2
        assert not e.vault.calls
        assert e.find('Preview, supported languages and job status').focused
    e.scenario(scenario)


@pytest.mark.parametrize('name,value', (
    ('Maximum approved budget in USD; no default price', '1'),
    ('Advanced v3 glossary resource; blank for none', 'projects/1/locations/global/glossaries/not-local'),
    ('Target language code', 'auto'),
    ('Optional price per million characters in USD; your current contract rate', '2'),
))
def test_local_ui_rejects_unsupported_options_without_approval_or_text_transfer(local_handlers, name, value):
    e = local_handlers
    def scenario():
        prepare(e)
        e.find(name).ChangeValue(value)
        before = len(e.local.state['calls']), len(e.state['approvals'])
        e.press('Preview')
        e.press('Translate / resume')
        assert (len(e.local.state['calls']), len(e.state['approvals'])) == before
        assert not e.service.jobs.identities() and not e.state['queue']
    e.scenario(scenario)


def test_local_ui_closing_before_worker_retains_paused_exact_job_without_transfer(local_handlers):
    e = local_handlers
    def scenario():
        prepare(e)
        e.press('Preview')
        e.press('Translate / resume')
        before = len(e.local.state['calls'])
        e.press('Close')
        e.complete()
        job, = e.service.list()
        assert job['status'] == 'paused' and job['receipts'] == []
        assert job['source_text'] == 'Explicit local selected text.'
        assert len(e.local.state['calls']) == before and not e.vault.calls
    e.scenario(scenario)


def test_local_ui_profile_revocation_while_queued_refuses_transfer(local_handlers):
    e = local_handlers
    def scenario():
        prepare(e)
        e.press('Preview')
        e.press('Translate / resume')
        before = len(e.local.state['calls'])
        ProfileStore(e.frame.library.root / 'state/providers').save(replace(e.p, enabled=False))
        e.complete()
        job, = e.service.list()
        assert job['status'] == 'attention' and job['receipts'] == []
        assert len(e.local.state['calls']) == before and not e.vault.calls
        assert 'profile changed' in e.find('Preview, supported languages and job status').GetValue()
    e.scenario(scenario)


def test_local_ui_changed_review_cannot_transfer_old_or_new_text(local_handlers):
    e = local_handlers
    def scenario():
        prepare(e)
        e.press('Preview')
        e.find('Selected text; originals remain unchanged').ChangeValue('Changed after visible review.')
        before = len(e.local.state['calls']), len(e.state['approvals'])
        e.press('Translate / resume')
        assert not e.state['queue'] and not e.service.jobs.identities()
        assert (len(e.local.state['calls']), len(e.state['approvals'])) == before
        assert 'Preview this exact' in e.find('Preview, supported languages and job status').GetValue()
    e.scenario(scenario)


def test_local_cli_preview_rejects_remote_flag_and_nonzero_budget_without_calls(
        local_handlers, tmp_path, capsys):
    e = local_handlers
    # Use the real service/cache from the actual local protocol fixture.
    source = tmp_path / 'selected.txt'
    source.write_text('CLI explicit source', encoding='utf-8')
    base = ['--data-dir', str(e.frame.library.root)]
    assert translation_cli.main(base + ['languages', '--profile', e.p.id, '--approve-remote']) == 1
    capsys.readouterr()
    before = len(e.local.state['calls'])
    assert translation_cli.main(base + ['languages', '--profile', e.p.id, '--approve-local']) == 0
    capsys.readouterr()
    options = ['--profile', e.p.id, '--input', str(source), '--source', 'en', '--target', 'uk']
    assert translation_cli.main(base + ['preview', *options, '--budget', '1']) == 1
    assert 'zero external cost' in capsys.readouterr().err
    assert len(e.local.state['calls']) == before + 1
