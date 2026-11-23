import json
import sqlite3
from pathlib import Path, PureWindowsPath

import pytest

from app.maintenance import files


def test_desktop_session_tree_is_excluded_from_inventory_and_snapshot(tmp_path):
    source = tmp_path / 'source'
    (source / 'app/desktop/Cache').mkdir(parents=True)
    (source / 'app/desktop/Cache/open-file').write_text('cache')
    (source / 'app/desktop/Cookies').write_text('private cookie')
    (source / 'app/banxue.db').touch()
    (source / 'material.txt').write_text('keep')
    assert files.excluded(Path('app/desktop'))
    assert files.excluded(Path('app/desktop/Cache/open-file'))
    assert files.inventory(source) == [Path('app/banxue.db'), Path('material.txt')]

    stage = tmp_path / 'stage'
    stage.mkdir()
    files.snapshot_tree(source, stage)
    assert (stage / 'material.txt').read_text() == 'keep'
    assert not (stage / 'app/desktop').exists()


def test_windows_remap_accepts_both_separators_and_respects_boundaries():
    source = PureWindowsPath('C:/Users/Ada/AppData/Local/MinDock')
    target = PureWindowsPath('D:/Profile/MinDock')
    values = {
        'backslash': r'C:\Users\Ada\AppData\Local\MinDock\StudyPal\note.md',
        'slash': 'C:/Users/Ada/AppData/Local/MinDock/StudyPal/note.md',
        'mixed': r'c:/users/ada/AppData/Local/MinDock\StudyPal/note.md',
        'root': str(source),
        'sibling': r'C:\Users\Ada\AppData\Local\MinDock-old\note.md',
        'prefix': r'X:\C:\Users\Ada\AppData\Local\MinDock\note.md',
    }
    result = files.remap(values, source, target)
    for key in ('backslash', 'slash', 'mixed', 'root'):
        assert result[key].startswith(str(target))
    assert result['sibling'] == values['sibling']
    assert result['prefix'] == values['prefix']
    with pytest.raises(ValueError, match='traversal'):
        files.remap(r'C:\Users\Ada\AppData\Local\MinDock\..\outside.txt', source, target)


def test_relocate_windows_references_in_json_sqlite_and_notes(tmp_path):
    source = PureWindowsPath('C:/Users/Ada/AppData/Local/MinDock')
    target = PureWindowsPath('D:/Profile/MinDock')
    stage = tmp_path / 'stage'
    (stage / 'app').mkdir(parents=True)
    (stage / 'LifePal/notes').mkdir(parents=True)
    old = str(source) + r'\StudyPal\note.md'
    sibling = str(source) + r'-old\note.md'
    embedded = 'X:/' + old
    (stage / 'app/state.json').write_text(json.dumps({'paths': [old, sibling]}))
    note = stage / 'LifePal/notes/record.md'
    note.write_text(f'Source: {old}\nSibling: {sibling}\nEmbedded: {embedded}\n')
    db = stage / files.DB_REL
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE refs(path TEXT, source TEXT, details_json TEXT)')
        conn.execute('INSERT INTO refs VALUES(?,?,?)', (old, f'Material {old}', json.dumps({'path': old})))
    files.relocate(stage, source, target)
    new = str(target) + r'\StudyPal\note.md'
    assert json.loads((stage / 'app/state.json').read_text()) == {'paths': [new, sibling]}
    assert note.read_text() == f'Source: {new}\nSibling: {sibling}\nEmbedded: {embedded}\n'
    with sqlite3.connect(db) as conn:
        path, embedded, details = conn.execute('SELECT path, source, details_json FROM refs').fetchone()
    assert (path, embedded, json.loads(details)) == (new, f'Material {new}', {'path': new})
