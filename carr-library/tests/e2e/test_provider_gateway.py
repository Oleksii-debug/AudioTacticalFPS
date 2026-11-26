"""Provider permissions, key routing and durable uncertain-charge regressions.

Every request is synthetic. No credentials or paid services are used.
"""
from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from carr_library.providers import (BudgetLedger, DirectTransport, JobApproval,
    ProfileStore, ProviderError, ProviderGateway, ProviderProfile, ProviderRequest)


class MemoryVault:
    def __init__(self):
        self.values = {}

    def put(self, reference, binding, secret):
        self.values[reference, binding] = secret

    def get(self, reference, binding):
        try:
            return self.values[reference, binding]
        except KeyError:
            raise ProviderError('Reconnect this profile.') from None

    def delete(self, reference, binding):
        self.values.pop((reference, binding), None)


class Transport:
    def __init__(self):
        self.calls = []
        self.error = None
        self.response = b'{"choices":[{"message":{"content":"Synthetic response"}}]}'

    def post(self, profile, route, payload, headers, **kwargs):
        self.calls.append((profile.id, route, json.loads(payload), dict(headers)))
        if self.error:
            raise self.error
        return self.response


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = MemoryVault()
        self.store = ProfileStore(self.root / 'connections.json', self.vault)
        self.profile = self.store.save(ProviderProfile('writer', 'Writing account',
            'openai-compatible', 'https://example.com/v1', 'user-selected-model', ('llm',)),
            secret='synthetic-test-key-never-a-real-key')
        self.request = ProviderRequest('llm', 'Synthetic text only.')
        self.transport = Transport()
        self.ledger = BudgetLedger(self.root / 'budget.sqlite')
        self.gateway = ProviderGateway(self.ledger, vault=self.vault, transport=self.transport)

    def approval(self, profile=None, request=None, **kw):
        profile, request = profile or self.profile, request or self.request
        return JobApproval('job', profile.fingerprint, (request.fingerprint(profile),),
                           **{'budget_micros': 100, 'mode': 'APPROVED_REMOTE', **kw})

    def call(self, *, profile=None, request=None, approval=None, operation='first', estimate=50):
        return self.gateway.perform(profile or self.profile, request or self.request,
            approval=approval or self.approval(), operation_id=operation, estimated_micros=estimate)

    def test_local_only_never_resolves_secret_or_touches_transport(self):
        with patch.object(self.vault, 'get', side_effect=AssertionError('Secret accessed')):
            with self.assertRaisesRegex(ProviderError, 'LOCAL ONLY'):
                self.call(approval=self.approval(mode='LOCAL_ONLY'))
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.ledger.report('job'), [])

    def test_vault_exception_does_not_expose_credential_material(self):
        with patch.object(self.vault, 'get', side_effect=RuntimeError('synthetic-secret-material')):
            with self.assertRaises(ProviderError) as raised:
                self.call()
        self.assertNotIn('synthetic-secret-material', str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(self.transport.calls, [])

    def test_remote_task_binds_exact_data_and_profile(self):
        approval = self.approval()
        for changed in (replace(self.profile, endpoint='https://other.example/v1'),
                        replace(self.profile, model='different-model')):
            with self.assertRaisesRegex(ProviderError, 'outside the approved'):
                self.call(profile=changed, approval=approval)
        with self.assertRaisesRegex(ProviderError, 'outside the approved'):
            self.call(request=replace(self.request, text='Private library dump'), approval=approval)
        self.assertEqual(self.transport.calls, [])

    def test_multiple_accounts_have_distinct_bound_credentials(self):
        second = self.store.save(replace(self.profile, id='second', credential_ref=None), secret='second-test-key')
        self.call()
        self.call(profile=second, approval=replace(self.approval(second), job_id='second-job'), operation='second')
        self.assertEqual([call[3]['Authorization'] for call in self.transport.calls],
                         ['Bearer synthetic-test-key-never-a-real-key', 'Bearer second-test-key'])

    def test_profile_export_never_contains_keys_or_usable_references(self):
        saved = self.store.path.read_text()
        self.assertNotIn('synthetic-test-key', saved)
        exported = json.dumps(self.store.export_profiles())
        self.assertNotIn('url-api-', exported)
        self.assertNotIn('synthetic-test-key', exported)
        restored = ProfileStore(self.store.path, self.vault).profiles()
        self.assertEqual(restored, (self.profile,))

    def test_endpoint_edit_cannot_reuse_credential(self):
        with self.assertRaisesRegex(ProviderError, 'Reconnect'):
            self.store.save(replace(self.profile, endpoint='https://other.example/v1'))
        self.assertEqual(self.store.profiles(), (self.profile,))

    def test_remove_deletes_vault_credential(self):
        self.store.remove('writer')
        self.assertEqual(self.store.profiles(), ())
        self.assertEqual(self.vault.values, {})

    def test_uncertain_operation_survives_restart_and_cannot_rebill(self):
        self.transport.error = RuntimeError('provider leaked synthetic-test-key-never-a-real-key')
        with self.assertRaises(ProviderError) as raised:
            self.call()
        self.assertNotIn('synthetic-test-key', str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        again = ProviderGateway(BudgetLedger(self.root / 'budget.sqlite'), vault=self.vault, transport=self.transport)
        with self.assertRaisesRegex(ProviderError, 'already recorded'):
            again.perform(self.profile, self.request, approval=self.approval(), operation_id='first', estimated_micros=50)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.ledger.report('job')[0]['state'], 'uncertain')

    def test_budget_reservation_counts_uncertain_calls(self):
        self.transport.error = TimeoutError()
        approval = self.approval(max_calls=10)
        with self.assertRaises(ProviderError):
            self.call(approval=approval, estimate=75)
        with self.assertRaisesRegex(ProviderError, 'budget'):
            self.call(approval=approval, operation='second', estimate=75)
        self.assertEqual(len(self.transport.calls), 1)

    def test_budget_cannot_be_reset_by_recreating_approval(self):
        self.call()
        with self.assertRaisesRegex(ProviderError, 'approval changed'):
            self.call(approval=self.approval(budget_micros=10000), operation='second')

    def test_unknown_cost_requires_explicit_estimate(self):
        with self.assertRaisesRegex(ProviderError, 'explicit estimate'):
            self.call(estimate=None)
        self.assertEqual(self.transport.calls, [])

    def test_no_model_tool_calls_are_executed(self):
        self.transport.response = b'{"choices":[{"message":{"content":"Run shell", "tool_calls":[{}]}}]}'
        with self.assertRaises(ProviderError):
            self.call()
        self.assertEqual(self.ledger.report('job')[0]['state'], 'uncertain')

    def test_local_service_needs_exact_loopback_authorization(self):
        profile = ProviderProfile('local', 'Local model', 'openai-compatible',
                                  'http://127.0.0.1:8080/v1', 'local-model', ('llm',), local=True)
        approval = self.approval(profile, mode='LOCAL_ONLY', allow_loopback=False, budget_micros=0)
        with self.assertRaisesRegex(ProviderError, 'exact local service'):
            self.call(profile=profile, approval=approval, estimate=0)
        self.call(profile=profile, approval=replace(approval, allow_loopback=True), estimate=0)
        self.assertEqual(self.transport.calls[0][3], {})

    def test_remote_urls_and_vendor_host_confusion_rejected(self):
        for endpoint in ('http://example.com/v1', 'https://key@example.com/v1',
                         'https://example.com/v1?api_key=secret', 'https://example.com/a/../b'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ProviderError):
                replace(self.profile, endpoint=endpoint)
        for endpoint in ('https://api.elevenlabs.io.evil.example/v1', 'https://api.elevenlabs.io:444/v1'):
            with self.assertRaises(ProviderError):
                ProviderProfile('voice', 'Voice', 'elevenlabs', endpoint, 'test-model', ('tts',))

    def test_music_and_sound_effects_use_typed_documented_requests(self):
        profile = self.store.save(ProviderProfile('audio', 'Audio', 'elevenlabs',
            'https://api.elevenlabs.io/v1', 'music_v2_5', ('music', 'sound_effects')), secret='test-audio-key')
        request = ProviderRequest('music', 'Original quiet instrumental.', duration_seconds=10)
        self.transport.response = b'ID3synthetic-audio-not-quality-proof'
        self.call(profile=profile, request=request, approval=self.approval(profile, request))
        call = self.transport.calls[-1]
        self.assertEqual(call[1], '/music')
        self.assertEqual(call[2]['music_length_ms'], 10000)
        self.assertTrue(call[2]['force_instrumental'])
        self.assertEqual(call[3], {'xi-api-key': 'test-audio-key'})
        with self.assertRaises(ProviderError):
            ProviderRequest('sound_effects', 'Door', duration_seconds=100)

    def test_dns_scope_checked_before_opening_connection(self):
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]), \
                patch('http.client.HTTPConnection') as connect:
            with self.assertRaisesRegex(ProviderError, 'network scope'):
                DirectTransport().post(self.profile, '/chat/completions', b'{}', {})
            connect.assert_not_called()

    def test_redirects_are_not_followed_or_retried(self):
        profile = ProviderProfile('local', 'Local', 'openai-compatible',
                                  'http://127.0.0.1:8080/v1', 'local', ('llm',), local=True)
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 8080))]), \
                patch('http.client.HTTPConnection') as connect:
            connect.return_value.getresponse.return_value.status = 302
            with self.assertRaisesRegex(ProviderError, 'HTTP 302'):
                DirectTransport().post(profile, '/chat/completions', b'{}', {})
            self.assertEqual(connect.return_value.request.call_count, 1)
            connect.return_value.close.assert_called_once()

    def test_ledger_contains_hashes_estimates_not_user_text_or_secrets(self):
        self.call()
        report = self.ledger.report('job')
        self.assertEqual(report[0]['state'], 'complete')
        self.assertEqual(report[0]['estimate_micros'], 50)
        self.assertEqual(len(report[0]['output_sha256']), 64)
        with sqlite3.connect(self.ledger.path) as db:
            dump = '\n'.join(db.iterdump())
        self.assertNotIn(self.request.text, dump)
        self.assertNotIn('synthetic-test-key', dump)


if __name__ == '__main__':
    unittest.main()
