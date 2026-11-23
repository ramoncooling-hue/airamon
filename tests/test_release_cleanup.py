import json

from scripts.organize_release_outputs import organize


def test_cleanup_preserves_data_and_archives_with_manifest(tmp_path):
    for relative in ('output/guide/example.png', 'data/private.txt', 'output/models/model.bin',
                     'output/windows/current.exe', 'output/macos/current.dmg'):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'preserve')
    plan = organize(tmp_path)
    assert len(plan['moves']) == 1
    assert (tmp_path / 'output/guide/example.png').exists()
    organize(tmp_path, apply=True, stamp='test')
    assert (tmp_path / '_archive/test/output/guide/example.png').read_bytes() == b'preserve'
    report = json.loads((tmp_path / '_archive/test/manifest.json').read_text())
    assert report['moves'][0]['status'] == 'moved'
    for relative in ('data/private.txt', 'output/models/model.bin', 'output/windows/current.exe',
                     'output/macos/current.dmg'):
        assert (tmp_path / relative).read_bytes() == b'preserve'


def test_cleanup_rejects_symbolic_link(tmp_path):
    import pytest
    (tmp_path / 'output').mkdir()
    (tmp_path / 'outside').mkdir()
    (tmp_path / 'output/guide').symlink_to(tmp_path / 'outside', target_is_directory=True)
    with pytest.raises(ValueError, match='linked'):
        organize(tmp_path, apply=True)


def test_cleanup_never_infers_historical_builds_from_date(tmp_path):
    builds = ('output/macos/20261023-085901/build-result.json',
              'output/macos/20261024-120000/build-result.json',
              'output/macos/MinDock-next.dmg',
              'output/windows/MinDock-next-Setup.exe')
    for relative in builds:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'keep')
    assert organize(tmp_path, apply=True)['moves'] == []
    assert all((tmp_path / relative).read_bytes() == b'keep' for relative in builds)
