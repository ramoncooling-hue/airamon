"""T2.2/O-DATA-3: renaming a node rekeys its stable identity and bridges.

The rename must keep bridge_links/node keys pointing at the same concept and
survive an export -> import roundtrip, while a normalized-name collision still
fails with a clear ValueError instead of breaking UNIQUE constraints.
"""

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from app.memory import db, graph_tools, repo, search_index


class IdentityStabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app_a = self.root / 'app_a'
        self.app_b = self.root / 'app_b'

    def tearDown(self):
        self.tmp.cleanup()

    def _db(self, app_dir: Path):
        app_dir.mkdir(parents=True, exist_ok=True)
        stack = ExitStack()
        stack.enter_context(patch.object(db, 'APP_DIR', app_dir))
        stack.enter_context(patch.object(db, 'DB_PATH', app_dir / 'banxue.db'))
        return stack

    def _world(self) -> dict:
        db.apply_schema()
        conn = db.connect()
        try:
            with conn:
                conn.execute("INSERT INTO courses(name) VALUES('课程甲'),('课程乙')")
                course_a, course_b = [r[0] for r in conn.execute(
                    'SELECT id FROM courses ORDER BY id')]
                conn.execute(
                    "INSERT INTO kp(course_id,name,gist) VALUES(?,?,?),(?,?,?)",
                    (course_a, '旧名甲', '要点甲', course_b, '节点乙', '要点乙'))
                a_kp, b_kp = [r[0] for r in conn.execute(
                    'SELECT id FROM kp ORDER BY id')]
                conn.execute(
                    """INSERT INTO bridges(course_a,kp_a,course_b,kp_b,topic)
                       VALUES('课程甲','旧名甲','课程乙','节点乙','桥接')""")
        finally:
            conn.close()
        repo.sync_node_identities()
        repo.sync_bridge_links()
        return {'course_a': course_a, 'course_b': course_b,
                'a_kp': a_kp, 'b_kp': b_kp}

    def _bridge_link(self):
        conn = db.connect()
        try:
            return dict(conn.execute('SELECT * FROM bridge_links').fetchone())
        finally:
            conn.close()

    def test_rename_retargets_node_key_bridge_links_and_bridge_text(self):
        with self._db(self.app_a):
            world = self._world()
            old_key = repo.ensure_node_identity(world['a_kp'])['node_key']
            result = repo.update_kp(world['a_kp'], {'name': '新名甲'}, origin='test')
            self.assertEqual(result['changed'], ['name'])
            self.assertEqual(result['kp']['name'], '新名甲')
            new_key = repo.ensure_node_identity(world['a_kp'])['node_key']
            self.assertNotEqual(old_key, new_key)
            self.assertEqual(new_key, repo._node_key(world['course_a'], '新名甲'))
            conn = db.connect()
            try:
                self.assertEqual(conn.execute(
                    'SELECT COUNT(*) FROM kp_identities WHERE node_key=?',
                    (old_key,)).fetchone()[0], 0, '旧 name 的 identity 必须移除')
                self.assertEqual(conn.execute(
                    'SELECT node_key FROM kp_identities WHERE kp_id=?',
                    (world['a_kp'],)).fetchone()[0], new_key)
                link = self._bridge_link()
                self.assertEqual(link['a_kp_id'], world['a_kp'])
                self.assertEqual(link['a_node_key'], new_key)
                self.assertEqual(link['b_node_key'],
                                 repo._node_key(world['course_b'], '节点乙'))
                self.assertEqual(link['status'], 'resolved')
                self.assertEqual(conn.execute(
                    'SELECT kp_a FROM bridges').fetchone()[0], '新名甲')
                revision = conn.execute(
                    "SELECT field,old_value,new_value FROM revisions "
                    "WHERE kp_id=? AND field='name'", (world['a_kp'],)).fetchone()
                self.assertEqual(tuple(revision), ('name', '旧名甲', '新名甲'))
                self.assertEqual(conn.execute(
                    'SELECT alias FROM kp_aliases WHERE kp_id=?',
                    (world['a_kp'],)).fetchone()[0], '旧名甲')
            finally:
                conn.close()
            # An explicit maintenance sync must not undo the retargeted relation.
            repo.sync_bridge_links()
            rows = repo.list_bridges('课程甲')
            self.assertEqual(rows[0]['status'], 'resolved')
            self.assertEqual(rows[0]['a_node_key'], new_key)

    def test_rename_survives_export_import_roundtrip(self):
        with self._db(self.app_a):
            world = self._world()
            repo.update_kp(world['a_kp'], {'name': '新名甲'}, origin='test')
            key_a = repo.ensure_node_identity(world['a_kp'])['node_key']
            bridge_a = repo.list_bridges('课程甲')[0]
            self.assertEqual(bridge_a['a_node_key'], key_a)
            payload = graph_tools.export_graph()
            exported = {node['name'] for node in payload['nodes']}
            self.assertIn('新名甲', exported)
            self.assertNotIn('旧名甲', exported)
        with self._db(self.app_b):
            db.apply_schema()
            conn = db.connect()
            try:
                with conn:
                    conn.execute("INSERT INTO courses(name) VALUES('课程甲'),('课程乙')")
            finally:
                conn.close()
            imported = graph_tools.import_graph(payload)
            self.assertEqual(imported['nodes_created'], 2)
            conn = db.connect()
            try:
                imported_id = conn.execute(
                    "SELECT id FROM kp WHERE name='新名甲'").fetchone()[0]
            finally:
                conn.close()
            key_b = repo.ensure_node_identity(imported_id)['node_key']
            self.assertEqual(key_b, key_a, 'export→import 后 node_key 必须一致')
            # The imported concept still resolves to the same node key the
            # bridge points at, so the relation target stays continuous.
            conn = db.connect()
            try:
                self.assertEqual(conn.execute(
                    'SELECT COUNT(*) FROM kp_identities WHERE node_key=?',
                    (key_a,)).fetchone()[0], 1)
            finally:
                conn.close()

    def test_normalized_name_conflict_raises_without_side_effects(self):
        with self._db(self.app_a):
            world = self._world()
            conn = db.connect()
            try:
                with conn:
                    conn.execute("INSERT INTO kp(course_id,name) VALUES(?,?)",
                                 (world['course_a'], 'Cost'))
                other_id = conn.execute(
                    "SELECT id FROM kp WHERE name='Cost'").fetchone()[0]
            finally:
                conn.close()
            repo.sync_node_identities()
            before_key = repo.ensure_node_identity(world['a_kp'])['node_key']
            with self.assertRaises(ValueError) as error:
                repo.update_kp(world['a_kp'], {'name': 'cost'}, origin='test')
            self.assertIn('同名', str(error.exception))
            conn = db.connect()
            try:
                self.assertEqual(conn.execute(
                    'SELECT name FROM kp WHERE id=?', (world['a_kp'],)).fetchone()[0],
                    '旧名甲')
                self.assertEqual(conn.execute(
                    'SELECT node_key FROM kp_identities WHERE kp_id=?',
                    (world['a_kp'],)).fetchone()[0], before_key)
                self.assertEqual(conn.execute(
                    'SELECT COUNT(*) FROM revisions WHERE kp_id=?',
                    (world['a_kp'],)).fetchone()[0], 0)
                self.assertEqual(conn.execute(
                    'SELECT COUNT(*) FROM kp_aliases WHERE kp_id=?',
                    (world['a_kp'],)).fetchone()[0], 0)
                with conn:
                    conn.execute('DELETE FROM kp_identities WHERE kp_id=?', (other_id,))
            finally:
                conn.close()
            # Unsyc'd nodes are covered by the deterministic-key comparison too.
            with self.assertRaises(ValueError):
                repo.update_kp(world['a_kp'], {'name': ' COST '}, origin='test')

    def test_non_rename_edits_keep_identity_and_bridges_stable(self):
        with self._db(self.app_a):
            world = self._world()
            key = repo.ensure_node_identity(world['a_kp'])['node_key']
            bridge_key = self._bridge_link()['a_node_key']
            result = repo.update_kp(world['a_kp'], {'gist': '新要点'}, origin='test')
            self.assertEqual(result['changed'], ['gist'])
            self.assertEqual(repo.ensure_node_identity(world['a_kp'])['node_key'], key)
            same = repo.update_kp(world['a_kp'], {'name': '旧名甲'}, origin='test')
            self.assertEqual(same['changed'], [])
            self.assertEqual(repo.ensure_node_identity(world['a_kp'])['node_key'], key)
            self.assertEqual(self._bridge_link()['a_node_key'], bridge_key)
            repo.update_kp(world['a_kp'], {'name': '中间名'}, origin='test')
            repo.update_kp(world['a_kp'], {'name': '最终名'}, origin='test')
            final_key = repo.ensure_node_identity(world['a_kp'])['node_key']
            self.assertEqual(final_key, repo._node_key(world['course_a'], '最终名'))
            conn = db.connect()
            try:
                keys = [row[0] for row in conn.execute(
                    'SELECT node_key FROM kp_identities WHERE kp_id=?',
                    (world['a_kp'],)).fetchall()]
            finally:
                conn.close()
            self.assertEqual(keys, [final_key])

    def test_renamed_node_is_searchable_and_old_name_is_gone(self):
        with self._db(self.app_a):
            world = self._world()
            repo.update_kp(world['a_kp'], {'name': '新名甲'}, origin='test')
            hits = search_index.search('新名甲')
            self.assertEqual([(row['kind'], row['ref_id']) for row in hits],
                             [('kp', str(world['a_kp']))])
            self.assertEqual(search_index.search('旧名甲'), [])


if __name__ == '__main__':
    unittest.main()
