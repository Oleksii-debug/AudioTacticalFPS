from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from carr_library.providers import ProviderProfile, ProviderRequest
from carr_library.ui.api_connections import ConnectionsController, parse_budget


class LocalTransport:
    def __init__(self):
        self.called = 0

    def post(self, profile, route, body, headers, **kwargs):
        self.called += 1
        return b'{"choices":[{"message":{"content":"Local generated text"}}]}'


class ConnectionControllerTests(unittest.TestCase):
    def test_decimal_budget_is_exact(self):
        self.assertEqual(parse_budget('0.012345'), 12345)
        for value in ('NaN', 'Infinity', '-1', '10001', '0.0000001', 'not money'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_budget(value)

    def test_profile_does_not_enable_remote_calls_and_output_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'private'
            root.mkdir()
            transport = LocalTransport()
            controller = ConnectionsController(root, transport=transport)
            profile = ProviderProfile('local', 'Local model', 'openai-compatible',
                'http://127.0.0.1:8080/v1', 'local-model', ('llm',), local=True)
            controller.store.save(profile)
            self.assertEqual(transport.called, 0)
            request = ProviderRequest('llm', 'Synthetic task')
            target = Path(temp) / 'result.txt'
            with self.assertRaises(ValueError):
                controller.run(profile, request, target, estimate_micros=0)
            self.assertEqual(transport.called, 0)
            result = controller.run(profile, request, target, approved=True, estimate_micros=0)
            self.assertEqual(target.read_text(), 'Local generated text')
            self.assertEqual(result['billed_cost'], 'unknown')
            with self.assertRaises(FileExistsError):
                controller.run(profile, request, target, approved=True, estimate_micros=0)
            self.assertEqual(transport.called, 1)

    def test_output_is_checked_before_any_billable_request(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'private'
            root.mkdir()
            transport = LocalTransport()
            controller = ConnectionsController(root, transport=transport)
            profile = ProviderProfile('local', 'Local model', 'openai-compatible',
                'http://127.0.0.1:8080/v1', 'local-model', ('llm',), local=True)
            request = ProviderRequest('llm', 'Synthetic task')
            for output in (root / 'library.sqlite', Path(temp) / 'CON.txt', Path(temp) / 'audio:secret.mp3'):
                with self.subTest(output=output), self.assertRaises(ValueError):
                    controller.run(profile, request, output, approved=True, estimate_micros=0)
            with patch('tempfile.mkstemp', side_effect=OSError('full disk')):
                with self.assertRaises(OSError):
                    controller.run(profile, request, Path(temp) / 'result.txt', approved=True, estimate_micros=0)
            self.assertEqual(transport.called, 0)

    def test_post_generation_disk_failure_warns_against_rebilling(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'private'
            root.mkdir()
            transport = LocalTransport()
            controller = ConnectionsController(root, transport=transport)
            profile = ProviderProfile('local', 'Local', 'openai-compatible',
                'http://127.0.0.1:8080/v1', 'model', ('llm',), local=True)
            with patch('os.fsync', side_effect=OSError('full disk')):
                with self.assertRaisesRegex(ValueError, 'do not repeat the paid request'):
                    controller.run(profile, ProviderRequest('llm', 'Test'), Path(temp) / 'result.txt',
                                   approved=True, estimate_micros=0)
            self.assertEqual(transport.called, 1)


if __name__ == '__main__':
    unittest.main()
