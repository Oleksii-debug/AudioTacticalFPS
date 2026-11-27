"""Synthetic transport regressions; no sockets, real API keys or paid requests."""
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch, sentinel

from carr_library import providers
from carr_library.providers import DirectTransport, ProviderError, ProviderProfile


class ProviderTransportBoundaryTests(unittest.TestCase):
    def profile(self, *, tls=False):
        endpoint = 'https://127.0.0.1:8443/v1' if tls else 'http://127.0.0.1:8080/v1'
        return ProviderProfile('review-local', 'Synthetic local service',
                               'openai-compatible', endpoint, 'synthetic-model',
                               ('llm',), local=True)

    def connection(self):
        response = Mock(status=200)
        response.getheader.return_value = None
        response.read.side_effect = [b'{"ok":true}', b'']
        connection = Mock()
        connection.sock = sentinel.plain_socket
        connection.getresponse.return_value = response
        return connection

    def addresses(self, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', port))]

    def test_cancel_during_connect_sends_no_request(self):
        cancelled = threading.Event()
        connection = self.connection()
        connection.connect.side_effect = cancelled.set
        with patch.object(providers.socket, 'getaddrinfo', return_value=self.addresses(8080)), \
                patch.object(providers.http.client, 'HTTPConnection', return_value=connection):
            with self.assertRaises(ProviderError):
                DirectTransport().post(
                    self.profile(), '/chat/completions', b'{"synthetic":true}',
                    {'Authorization': 'Bearer synthetic-review-token'},
                    cancelled=cancelled.is_set)
        connection.request.assert_not_called()
        connection.getresponse.assert_not_called()
        connection.close.assert_called_once()

    def test_tls_keylog_environment_is_ignored_without_disabling_verification(self):
        connection = self.connection()
        contexts = []

        def capture(context):
            # Keep the real TLS policy, replacing only the handshake so the
            # regression never opens a socket or contacts a provider.
            context.wrap_socket = Mock(return_value=sentinel.tls_socket)
            contexts.append(context)
            return context

        tls = SimpleNamespace(
            PROTOCOL_TLS_CLIENT=ssl.PROTOCOL_TLS_CLIENT,
            SSLContext=lambda protocol: capture(ssl.SSLContext(protocol)),
            create_default_context=lambda: capture(ssl.create_default_context()))
        with tempfile.TemporaryDirectory(prefix='provider-tls-review-') as folder:
            keylog = Path(folder) / 'tls-keys.log'
            with patch.dict(os.environ, {'SSLKEYLOGFILE': str(keylog)}), \
                    patch.object(providers, 'ssl', tls), \
                    patch.object(providers.socket, 'getaddrinfo', return_value=self.addresses(8443)), \
                    patch.object(providers.http.client, 'HTTPConnection', return_value=connection):
                result = DirectTransport().post(
                    self.profile(tls=True), '/chat/completions', b'{"synthetic":true}',
                    {'Authorization': 'Bearer synthetic-review-token'})
            self.assertEqual(result, b'{"ok":true}')
            self.assertEqual(len(contexts), 1)
            context = contexts[0]
            self.assertIsNone(context.keylog_filename)
            self.assertFalse(keylog.exists())
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            context.wrap_socket.assert_called_once_with(
                sentinel.plain_socket, server_hostname='127.0.0.1')
        connection.request.assert_called_once()
        connection.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
