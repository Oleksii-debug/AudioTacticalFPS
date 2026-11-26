import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from probe import REQUIRED, evaluate, load_engine, verified_bundle

class ProbeTests(unittest.TestCase):
    def test_runtime_policy_precedes_optional_model_import(self):
        events = []
        engine = object()
        original_import = __import__

        def construct(**options):
            self.assertEqual(events, ['policy', 'import'])
            self.assertTrue(options['local_files_only'])
            events.append('construct')
            return engine

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == 'fastembed':
                self.assertEqual(events, ['policy'])
                events.append('import')
                return SimpleNamespace(TextEmbedding=construct)
            return original_import(name, globals, locals, fromlist, level)

        # Bundle validation has its own byte-level tests. No optional runtime
        # is loaded here: the import hook checks ordering before returning it.
        with patch('probe.verified_bundle', return_value='verified-local-model'), \
                patch('carr_library.offline_policy.disable_native_telemetry',
                      side_effect=lambda: events.append('policy')), \
                patch('builtins.__import__', side_effect=guarded_import):
            self.assertIs(load_engine('unused', {}), engine)
        self.assertEqual(events, ['policy', 'import', 'construct'])

        events.clear()
        with patch('probe.verified_bundle', return_value='verified-local-model'), \
                patch('carr_library.offline_policy.disable_native_telemetry',
                      side_effect=RuntimeError('Restart before loading the runtime')), \
                patch('builtins.__import__', side_effect=guarded_import):
            with self.assertRaisesRegex(RuntimeError, 'Restart'):
                load_engine('unused', {})
        self.assertEqual(events, [])

    def test_rank(self):
        class Fake:
            def embed(self, texts, batch_size):
                return [[a,b]+[0]*382 for a,b in [(1,0),(1,0),(1,0),(0,1),(0,1)]]
        self.assertEqual(evaluate(Fake())['ranked_ids'], [0,1,2,3])
        self.assertEqual(evaluate(Fake())['recall_at_2'], 1)
    def test_bad_vector(self):
        class Fake:
            def embed(self, texts, batch_size):
                return [[0]*384]*5
        with self.assertRaises(ValueError):
            evaluate(Fake())
    def test_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name in REQUIRED:
                (root/name).write_bytes(b'synthetic')
            hashes = {name:hashlib.sha256(b'synthetic').hexdigest() for name in REQUIRED}
            self.assertEqual(verified_bundle(root, hashes), str(root.resolve()))
            (root/'config.json').write_bytes(b'changed')
            with self.assertRaises(ValueError):
                verified_bundle(root, hashes)
    def test_incomplete(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                verified_bundle(d, {})
    def test_unexpected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name in REQUIRED | {'extra'}:
                (root/name).write_bytes(b'synthetic')
            hashes = {name:hashlib.sha256(b'synthetic').hexdigest() for name in REQUIRED}
            with self.assertRaises(ValueError):
                verified_bundle(root, hashes)

if __name__ == '__main__':
    unittest.main()
