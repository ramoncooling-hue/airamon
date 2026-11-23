import sqlite3

from scripts.graph_baseline import inventory, readonly_snapshot


def test_inventory_separates_workspace_scope_and_reports_unbroken_db(tmp_path):
    path = tmp_path / 'db.sqlite'
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE workspaces(id INTEGER PRIMARY KEY,key TEXT,domain TEXT);
            CREATE TABLE courses(id INTEGER PRIMARY KEY,name TEXT,workspace_id INTEGER);
            CREATE TABLE kp(id INTEGER PRIMARY KEY,course_id INTEGER,deleted_at TEXT DEFAULT '');
            CREATE TABLE kp_edges(id INTEGER PRIMARY KEY,from_kp INTEGER,to_kp INTEGER,kind TEXT);
            CREATE TABLE knowledge_relation_meta(edge_id INTEGER,status TEXT);
            CREATE TABLE source_files(id INTEGER PRIMARY KEY,course_id INTEGER,source_kind TEXT);
            INSERT INTO workspaces VALUES(1,'studypal','mba'),(2,'workpal','work');
            INSERT INTO courses VALUES(1,'经济学',1),(2,'项目',2);
            INSERT INTO kp(id,course_id) VALUES(1,1),(2,2);
            INSERT INTO kp_edges VALUES(1,1,2,'related');
            INSERT INTO knowledge_relation_meta VALUES(1,'inferred');
            INSERT INTO source_files VALUES(1,1,'textbook');
        ''')
        result = inventory(conn)
    assert result['nodes_by_workspace'] == [['studypal', 1], ['workpal', 1]]
    assert result['cross_workspace_edges'] == [[1]]
    assert result['relations_by_status'] == [['inferred', 1]]
    assert result['integrity'] == [['ok']]


def test_readonly_snapshot_does_not_mutate_original_database(tmp_path):
    original = tmp_path / 'original.sqlite'
    snapshot = tmp_path / 'snapshot.sqlite'
    with sqlite3.connect(original) as conn:
        conn.execute('CREATE TABLE sample(value TEXT)')
        conn.execute("INSERT INTO sample VALUES('keep')")
    readonly_snapshot(original, snapshot)
    with sqlite3.connect(snapshot) as conn:
        assert conn.execute('SELECT value FROM sample').fetchall() == [('keep',)]
        conn.execute("INSERT INTO sample VALUES('isolated')")
    with sqlite3.connect(original) as conn:
        assert conn.execute('SELECT value FROM sample').fetchall() == [('keep',)]
