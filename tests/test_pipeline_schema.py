import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db


class PipelineSchemaTests(unittest.TestCase):
    def test_pipeline_schema_is_idempotent_and_has_required_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                first = db.apply_schema()
                second = db.apply_schema()
                self.assertIn('006_knowledge_pipeline.sql', first)
                self.assertIn('006_knowledge_pipeline.sql', second)
                tables = db.table_counts()
                for name in ('evidence', 'knowledge_notes', 'relation_candidates',
                             'event_revisions', 'skills', 'agents', 'agent_skills'):
                    self.assertIn(name, tables)


if __name__ == '__main__':
    unittest.main()
