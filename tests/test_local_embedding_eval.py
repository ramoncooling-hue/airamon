from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import local_embedding_eval as local


class OfflineEmbeddingTests(unittest.TestCase):
    def test_missing_model_fails_without_network_or_library_import(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(local.urllib.request, 'urlopen', side_effect=AssertionError('network')):
            with self.assertRaises(ValueError):
                local.OfflineEmbedding(directory)

    def test_model_loading_is_local_only_and_identity_changes_with_content(self):
        engine = Mock()
        fake = Mock(TextEmbedding=Mock(return_value=engine))
        with patch.object(local, 'verified', return_value=True), \
             patch.dict('sys.modules', {'fastembed': fake}), \
             patch.object(local.importlib.metadata, 'version', return_value='0.9.0'):
            first = local.OfflineEmbedding('/tmp/fixture')
            kwargs = fake.TextEmbedding.call_args.kwargs
            self.assertTrue(kwargs['local_files_only'])
            self.assertEqual(kwargs['providers'], ['CPUExecutionProvider'])
            self.assertEqual(kwargs['specific_model_path'], str(Path('/tmp/fixture').resolve()))
            changed = local.manifest()
            changed['files']['model_optimized.onnx']['sha256'] = 'changed'
            with patch.object(local, 'manifest', return_value=changed):
                second = local.OfflineEmbedding('/tmp/fixture')
            self.assertNotEqual(first.identity, second.identity)

    def test_checksum_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture'
            path.write_bytes(b'abc')
            entry = {'bytes':3, 'sha256':local.hashlib.sha256(b'abc').hexdigest()}
            self.assertTrue(local.verified(path, entry))
            entry['sha256'] = 'wrong'
            self.assertFalse(local.verified(path, entry))
            alias = Path(directory) / 'alias'
            alias.symlink_to(path)
            self.assertFalse(local.verified(alias, entry))

    def test_missing_model_comparison_is_incomplete_and_isolated(self):
        from scripts.evaluate_retrieval import run
        with tempfile.TemporaryDirectory() as directory:
            report = run(mode='onnx', model_dir=Path(directory) / 'absent')
        self.assertEqual(report['summary']['vector']['status'], 'incomplete')
        self.assertIsNone(report['summary']['vector']['recall_at_k'])
        self.assertIn('elapsed_seconds', report['summary']['keyword'])

    def test_hybrid_requires_explicit_finite_threshold_before_worker_launch(self):
        from scripts.evaluate_retrieval import run
        for value in (None, True, float('nan'), float('inf'), -0.1, 1.1):
            with self.assertRaises(ValueError):
                run(mode='hybrid', model_dir='/absent', min_similarity=value)

    def test_hybrid_failed_model_is_not_successful_negative_rejection(self):
        from scripts.evaluate_retrieval import run
        with tempfile.TemporaryDirectory() as directory:
            report = run(mode='hybrid', model_dir=Path(directory) / 'absent', min_similarity=.6)
        self.assertEqual(report['summary']['hybrid']['status'], 'incomplete')
        self.assertEqual(report['summary']['hybrid']['evaluated'], 0)
        self.assertTrue(all(r['hybrid']['status'] == 'degraded' for r in report['cases']))
        self.assertTrue(all(r['hybrid']['decision'] == 'unavailable' for r in report['cases']))
        self.assertEqual(report['metadata']['hybrid']['min_similarity'], .6)
        self.assertFalse(report['metadata']['hybrid']['answerability_checked'])

