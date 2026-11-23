import unittest
import json
from unittest.mock import patch

from app.settings import store


class WebConfigTests(unittest.TestCase):
    def _config(self, provider, key=''):
        cfg = json.loads(json.dumps(store.DEFAULTS))
        cfg['web'] = {'provider': provider, 'api_key': key}
        return cfg

    def test_bing_rss_is_ready_without_key(self):
        with patch.object(store, 'read_raw', return_value=self._config('bing_rss')):
            web = store.read_public()['web']
        self.assertTrue(web['ready'])
        self.assertFalse(web['requires_key'])
        self.assertEqual(web['status'], '免 Key 可用')

    def test_api_provider_requires_key(self):
        with patch.object(store, 'read_raw', return_value=self._config('tavily')):
            web = store.read_public()['web']
        self.assertFalse(web['ready'])
        self.assertTrue(web['requires_key'])
        self.assertEqual(web['status'], '需要 API Key')


if __name__ == '__main__':
    unittest.main()
