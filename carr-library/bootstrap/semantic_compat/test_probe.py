import hashlib
import tempfile
import unittest
from pathlib import Path
from probe import REQUIRED, evaluate, verified_bundle

class ProbeTests(unittest.TestCase):
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
