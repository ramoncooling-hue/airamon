import copy
import json
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent import llm
from app.settings import store


def profile(id='alpha', **changes):
    return dict(id=id, name='Example', provider='openai-compatible',
                base_url='https://provider.example/v1', model='selected-model',
                **changes)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    from app.api.model_routes import router
    from app.settings import model_profiles

    monkeypatch.setattr(store, 'APP_DIR', tmp_path / 'app')
    monkeypatch.setattr(store, 'SETTINGS_PATH', tmp_path / 'app/settings.json')
    app = FastAPI()
    app.include_router(router, prefix='/api')
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 12345)) as client:
        yield client, model_profiles


def put(client, profiles, routes=None, revision=None):
    if revision is None:
        revision = client.get('/api/settings/models').json()['revision']
    return client.put('/api/settings/models', json={
        'profiles': profiles, 'routes': routes or {'fast': '', 'reasoning': ''},
        'revision': revision,
    })


def seed_legacy():
    store.write({'fast': {'base_url': 'https://legacy.example/v1', 'model': 'legacy',
                          'api_key': 'legacy-private-key'},
                 'retrieval': {'top_k': 9}, 'custom': {'keep': [1, 2]}})


def test_crud_keeps_legacy_and_masks_every_public_response(isolated):
    client, models = isolated
    seed_legacy()
    initial = client.get('/api/settings/models').json()
    assert initial == {'profiles': [], 'routes': {'fast': '', 'reasoning': ''},
                       'revision': initial['revision']}
    original = store.read_raw()
    response = put(client, [profile(api_key='new-private-key')], {'fast': 'alpha', 'reasoning': ''})
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['profiles'][0]['api_key_set'] is True
    assert saved['profiles'][0]['api_key'] == store._mask('new-private-key')
    assert client.get('/api/settings/models').json() == saved
    assert models.read() == saved
    raw = store.read_raw()
    for key, value in original.items():
        if key not in ('model_profiles', 'model_routes'):
            assert raw[key] == value
    assert raw['model_profiles'][0]['api_key'] == 'new-private-key'
    assert raw['model_routes']['fast'] == 'alpha'
    assert store.SETTINGS_PATH.stat().st_mode & 0o777 == 0o600
    assert store.configured() == {'fast': True, 'reasoning': False}
    for public in (saved, store.read_public(), store.write({'retrieval': {'top_k': 10}})):
        text = json.dumps(public)
        assert 'new-private-key' not in text
        assert 'legacy-private-key' not in text


@pytest.mark.parametrize('key', [None, '', 'masked'])
def test_secret_preservation_is_by_id_not_position(isolated, key):
    client, _ = isolated
    saved = put(client, [profile(api_key='alpha-secret'), profile('beta', api_key='beta-secret')]).json()
    candidates = list(reversed(saved['profiles']))
    for candidate in candidates:
        if key is None:
            candidate.pop('api_key')
        elif key == '':
            candidate['api_key'] = ''
    response = put(client, candidates, revision=saved['revision'])
    assert response.status_code == 200, response.text
    assert {p['id']: p['api_key'] for p in store.read_raw()['model_profiles']} == {
        'alpha': 'alpha-secret', 'beta': 'beta-secret'}


def test_new_id_never_inherits_masked_key_and_explicit_clear_works(isolated):
    client, _ = isolated
    saved = put(client, [profile(api_key='alpha-secret')]).json()
    renamed = {**saved['profiles'][0], 'id': 'new-id'}
    response = put(client, [renamed])
    assert response.status_code == 200, response.text
    assert store.read_raw()['model_profiles'][0]['api_key'] == ''
    assert response.json()['profiles'][0]['api_key_set'] is False
    saved = put(client, [profile(api_key='replacement-secret')]).json()
    response = put(client, [{**saved['profiles'][0], 'clear_api_key': True}])
    assert response.status_code == 200
    assert store.read_raw()['model_profiles'][0]['api_key'] == ''
    assert 'clear_api_key' not in store.read_raw()['model_profiles'][0]


def test_referenced_deletion_rejected_and_unbound_deletion_allowed(isolated):
    client, _ = isolated
    put(client, [profile()], {'fast': 'alpha', 'reasoning': 'alpha'})
    before = store.SETTINGS_PATH.read_bytes()
    assert put(client, [], {'fast': 'alpha', 'reasoning': ''}).status_code == 422
    assert store.SETTINGS_PATH.read_bytes() == before
    assert put(client, []).status_code == 200


def test_revision_conflicts_include_legacy_writes_and_cannot_lose_profiles(isolated):
    client, _ = isolated
    initial = client.get('/api/settings/models').json()
    saved = put(client, [profile(api_key='alpha-secret')]).json()
    assert put(client, [], revision=initial['revision']).status_code == 409
    store.write({'fast': {'model': 'legacy-edit'}})
    assert put(client, [], revision=saved['revision']).status_code == 409
    assert store.read_raw()['model_profiles'][0]['api_key'] == 'alpha-secret'
    assert store.read_raw()['fast']['model'] == 'legacy-edit'


def test_concurrent_cas_has_one_winner(isolated):
    _, models = isolated
    revision = models.read()['revision']

    def save(id):
        try:
            models.save([profile(id)], {'fast': id, 'reasoning': ''}, revision)
            return 'saved'
        except store.RevisionConflict:
            return 'conflict'

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, ('alpha', 'beta'))) == ['conflict', 'saved']


def _process_save(path, revision, id, start, result):
    from app.settings import model_profiles
    store.SETTINGS_PATH = Path(path)
    start.wait(5)
    try:
        model_profiles.save([profile(id)], {'fast': id, 'reasoning': ''}, revision)
        result.send('saved')
    except store.RevisionConflict:
        result.send('conflict')
    finally:
        result.close()


def test_cas_is_shared_by_separate_backend_processes(isolated):
    _, models = isolated
    revision = models.read()['revision']
    context = multiprocessing.get_context('spawn')
    start = context.Event()
    processes, readers = [], []
    try:
        for id in ('alpha', 'beta'):
            reader, writer = context.Pipe(duplex=False)
            process = context.Process(target=_process_save,
                args=(str(store.SETTINGS_PATH), revision, id, start, writer))
            process.start()
            writer.close()
            readers.append(reader)
            processes.append(process)
        start.set()
        assert all(reader.poll(10) for reader in readers)
        assert sorted(reader.recv() for reader in readers) == ['conflict', 'saved']
    finally:
        for reader in readers:
            reader.close()
        for process in processes:
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)
            assert process.exitcode == 0
            process.close()


@pytest.mark.parametrize('reserved', ['model_profiles', 'model_routes'])
def test_legacy_write_rejects_model_keys_before_any_change(isolated, reserved):
    seed_legacy()
    before = store.SETTINGS_PATH.read_bytes()
    with pytest.raises(ValueError):
        store.write({'fast': {'model': 'changed'}, reserved: []})
    assert store.SETTINGS_PATH.read_bytes() == before


@pytest.mark.parametrize('change', [
    {'profiles': None}, {'profiles': {}}, {'profiles': [None]},
    {'profiles': [profile(), profile()]}, {'profiles': [profile(id='')]},
    {'profiles': [profile(id='../bad')]}, {'profiles': [profile(id=1)]},
    {'profiles': [{**profile(), 'name': ''}]},
    {'profiles': [{**profile(), 'model': False}]},
    {'profiles': [{**profile(), 'provider': {}}]},
    {'profiles': [{**profile(), 'api_key': None}]},
    {'profiles': [{**profile(), 'api_key_set': 'yes'}]},
    {'profiles': [{**profile(), 'clear_api_key': 1}]},
    {'profiles': [{**profile(), 'unexpected': 'secret-input'}]},
    {'routes': None}, {'routes': []}, {'routes': {'fast': ''}},
    {'routes': {'fast': '', 'reasoning': '', 'unknown': ''}},
    {'routes': {'fast': 'missing', 'reasoning': ''}},
    {'routes': {'fast': None, 'reasoning': ''}},
    {'revision': None}, {'unexpected': 'secret-input'},
])
def test_bad_structure_does_not_overwrite_or_echo_input(isolated, change):
    client, _ = isolated
    saved = put(client, [profile(api_key='alpha-secret')]).json()
    before = store.SETTINGS_PATH.read_bytes()
    body = {'profiles': saved['profiles'], 'routes': saved['routes'], 'revision': saved['revision']}
    response = client.put('/api/settings/models', json={**body, **change})
    assert response.status_code in (409, 422), response.text
    assert 'secret-input' not in response.text
    assert 'alpha-secret' not in response.text
    assert store.SETTINGS_PATH.read_bytes() == before


@pytest.mark.parametrize('url', [
    '', 'provider.example/v1', 'file:///tmp/private', 'ftp://provider.example',
    'https://user:secret-input@provider.example/v1', 'https://provider.example?key=secret-input',
    'https://provider.example/#secret-input', 'http://', 'https://provider.example:99999',
    'https://provider.example:bad', 'https://provider.example/\nsecret-input',
    'https://provider.example\\@evil.example', 'https://bad host/v1',
    'https://%zz/v1', 'https://bad..host/v1', 'https://-bad.example/v1',
    'https://provider.example:/v1', 'https://provider.example/%zz',
])
def test_invalid_url_is_rejected_without_echo(isolated, url):
    client, _ = isolated
    response = put(client, [{**profile(), 'base_url': url}])
    assert response.status_code == 422, response.text
    assert 'secret-input' not in response.text
    assert not store.SETTINGS_PATH.exists()


@pytest.mark.parametrize('url', ['http://localhost:11434/v1', 'http://127.0.0.1:8080/v1',
                                  'http://[::1]:8080/v1', 'https://provider.example/v1/chat/completions'])
def test_valid_compatible_endpoints_do_not_require_a_key(isolated, url):
    client, _ = isolated
    assert put(client, [{**profile(), 'base_url': url}], {'fast': 'alpha', 'reasoning': ''}).status_code == 200
    assert llm.available('fast')


@pytest.mark.parametrize('headers', [
    {'origin': 'https://evil.example'}, {'origin': 'null'},
    {'origin': 'http://localhost'}, {'host': 'evil.example'},
    {'sec-fetch-site': 'cross-site'},
])
def test_same_origin_and_loopback_guard_is_shared(isolated, headers):
    client, _ = isolated
    assert client.get('/api/settings/models', headers=headers).status_code == 403
    assert client.put('/api/settings/models', json={}, headers=headers).status_code == 403
    assert not store.SETTINGS_PATH.exists()


def test_foreign_client_and_unbounded_or_non_json_input_rejected(isolated):
    client, _ = isolated
    with TestClient(client.app, base_url='http://127.0.0.1', client=('192.168.1.8', 1234)) as remote:
        assert remote.get('/api/settings/models').status_code == 403
    assert client.get('/api/settings/models', headers={'origin': 'http://127.0.0.1'}).status_code == 200
    assert client.put('/api/settings/models', content='{}', headers={'content-type': 'text/plain'}).status_code == 415
    for text, status in [('x' * 65537, 413), ('[' * 15000 + '0' + ']' * 15000, 422),
                         ('{"profiles":[],"profiles":[]}', 422), ('[]', 422), ('not-json-secret', 422)]:
        response = client.put('/api/settings/models', content=text, headers={'content-type': 'application/json'})
        assert response.status_code == status
        assert 'not-json-secret' not in response.text


@pytest.mark.parametrize('where', ['file', 'parent', 'ancestor'])
def test_symlinks_are_never_followed(isolated, tmp_path, monkeypatch, where):
    client, _ = isolated
    external = tmp_path / 'outside'
    external.mkdir()
    target = external / 'settings.json'
    target.write_text('{"private": "external-secret"}')
    if where == 'file':
        store.SETTINGS_PATH.parent.mkdir()
        store.SETTINGS_PATH.symlink_to(target)
    elif where == 'parent':
        store.SETTINGS_PATH.parent.symlink_to(external, target_is_directory=True)
    else:
        (tmp_path / 'linked').symlink_to(external, target_is_directory=True)
        monkeypatch.setattr(store, 'SETTINGS_PATH', tmp_path / 'linked/nested/settings.json')
    assert client.get('/api/settings/models').status_code == 422
    assert put(client, [], revision='anything').status_code == 422
    with pytest.raises(ValueError):
        store.write({'web': {'provider': 'bing_rss'}})
    assert target.read_text() == '{"private": "external-secret"}'
    assert not (external / 'nested').exists()


@pytest.mark.parametrize('raw', ['broken-secret', '[]', '{"fast": 3}',
                               '{"model_profiles":null}', '{"model_routes":{"fast":"missing","reasoning":""}}'])
def test_corrupt_settings_are_not_replaced(isolated, raw):
    client, _ = isolated
    store.SETTINGS_PATH.parent.mkdir()
    store.SETTINGS_PATH.write_text(raw)
    response = client.get('/api/settings/models')
    assert response.status_code == 422
    assert 'broken-secret' not in response.text
    assert put(client, [], revision='anything').status_code == 422
    with pytest.raises(ValueError):
        store.write({'retrieval': {'top_k': 1}})
    assert store.SETTINGS_PATH.read_text() == raw


@pytest.mark.parametrize('failure', ['replace', 'fsync'])
def test_atomic_replace_failure_leaves_old_bytes_and_no_temporary_secret(isolated, monkeypatch, failure):
    client, _ = isolated
    saved = put(client, [profile(api_key='alpha-secret')]).json()
    original = store.SETTINGS_PATH.read_bytes()
    monkeypatch.setattr(store.os, failure, Mock(side_effect=OSError('sensitive-path')))
    response = put(client, [], revision=saved['revision'])
    assert response.status_code == 422
    assert 'sensitive-path' not in response.text
    assert store.SETTINGS_PATH.read_bytes() == original
    assert not list(store.SETTINGS_PATH.parent.glob('.settings-*'))


def test_legacy_write_failure_keeps_profiles_and_cleans_temporary_secret(isolated):
    client, _ = isolated
    put(client, [profile(api_key='alpha-secret')])
    before = store.SETTINGS_PATH.read_bytes()
    with patch.object(store.os, 'replace', side_effect=OSError('unavailable')):
        with pytest.raises(OSError):
            store.write({'fast': {'model': 'unsaved'}})
    assert store.SETTINGS_PATH.read_bytes() == before
    assert not list(store.SETTINGS_PATH.parent.glob('.settings-*'))


def test_lock_symlink_cannot_be_followed(isolated, tmp_path):
    client, _ = isolated
    outside = tmp_path / 'outside'
    outside.write_text('outside-secret')
    store.SETTINGS_PATH.parent.mkdir()
    (store.SETTINGS_PATH.parent / '.settings.lock').symlink_to(outside)
    assert put(client, [profile()], revision='anything').status_code == 422
    assert outside.read_text() == 'outside-secret'


def test_llm_available_payload_and_probe_use_selected_profile(isolated, monkeypatch):
    client, _ = isolated
    seed_legacy()
    put(client, [profile(api_key='selected-private-key')], {'fast': 'alpha', 'reasoning': ''})
    assert llm.available('fast')
    url, body, headers = llm._payload('fast', [], .2, False)
    assert url == 'https://provider.example/v1/chat/completions'
    assert body['model'] == 'selected-model'
    assert headers['Authorization'] == 'Bearer selected-private-key'
    transport = Mock(return_value=Mock(__enter__=lambda s: s, __exit__=lambda *a: None,
                                       read=lambda: b'{"choices":[{"message":{"content":"ok"}}]}'))
    monkeypatch.setattr(llm, '_open_completion', transport)
    probe = llm.probe('fast')
    assert probe['ok']
    assert probe['base_url'] == 'https://provider.example/v1'
    assert probe['model'] == 'selected-model'
    assert probe['key_set'] is True
    assert 'selected-private-key' not in json.dumps(probe)
    assert json.loads(transport.call_args.args[0].data)['model'] == 'selected-model'


def test_four_profiles_route_both_tiers_and_replacement_does_not_touch_legacy(isolated):
    client, _ = isolated
    seed_legacy()
    legacy = store.read_raw()['fast']
    profiles = [{**profile(f'model-{i}', api_key=f'private-key-{i}'),
                 'base_url': f'https://provider-{i}.example/v1', 'model': f'model-{i}'}
                for i in range(4)]
    response = put(client, profiles, {'fast': 'model-1', 'reasoning': 'model-3'})
    assert response.status_code == 200, response.text
    assert len(response.json()['profiles']) == 4
    for tier, index in [('fast', 1), ('reasoning', 3)]:
        url, body, headers = llm._payload(tier, [], .3, False)
        assert url == f'https://provider-{index}.example/v1/chat/completions'
        assert body['model'] == f'model-{index}'
        assert headers['Authorization'] == f'Bearer private-key-{index}'
    profiles[1]['api_key'] = 'replacement-key'
    response = put(client, profiles, {'fast': 'model-1', 'reasoning': 'model-3'})
    assert response.status_code == 200, response.text
    assert llm._payload('fast', [], .3, False)[2]['Authorization'] == 'Bearer replacement-key'
    assert store.read_raw()['fast'] == legacy


def test_unknown_tier_and_bad_selected_profile_do_not_use_transport(monkeypatch):
    cfg = {'fast': {'base_url': 'https://legacy.example', 'model': 'legacy'},
           'model_profiles': [{**profile(), 'base_url': 'file:///tmp/private'}],
           'model_routes': {'fast': 'alpha', 'reasoning': ''}}
    monkeypatch.setattr(store, 'read_raw', lambda: copy.deepcopy(cfg))
    transport = Mock(side_effect=AssertionError('no transport'))
    monkeypatch.setattr(llm, '_open_completion', transport)
    for tier in ('unknown', 'fast'):
        assert llm.available(tier) is False
        assert llm.probe(tier)['ok'] is False
    transport.assert_not_called()


@pytest.mark.parametrize('broken', [
    {'model_routes': {'fast': 'missing', 'reasoning': ''}},
    {'model_routes': {'fast': None, 'reasoning': ''}},
    {'model_routes': []}, {'model_profiles': None, 'model_routes': {'fast': 'alpha', 'reasoning': ''}},
    {'model_profiles': [profile(), profile()], 'model_routes': {'fast': 'alpha', 'reasoning': ''}},
])
def test_invalid_selection_fails_closed_even_with_valid_legacy(monkeypatch, broken):
    cfg = {'fast': {'base_url': 'https://legacy.example', 'model': 'legacy'}, **broken}
    monkeypatch.setattr(store, 'read_raw', lambda: copy.deepcopy(cfg))
    transport = Mock(side_effect=AssertionError('must not call legacy'))
    monkeypatch.setattr(llm, '_open_completion', transport)
    assert llm.available('fast') is False
    with pytest.raises(llm.LLMError):
        llm._payload('fast', [], .3, False)
    assert llm.probe('fast')['ok'] is False
    transport.assert_not_called()
    assert store.configured()['fast'] is False


def test_legacy_only_mock_and_empty_selection_keep_existing_behavior(monkeypatch):
    legacy = {'fast': {'base_url': 'http://127.0.0.1:11434/v1', 'model': 'legacy'}}
    for cfg in (legacy, {**legacy, 'model_profiles': [profile()],
                         'model_routes': {'fast': '', 'reasoning': ''}}):
        monkeypatch.setattr(store, 'read_raw', lambda: copy.deepcopy(cfg))
        assert llm.available('fast')
        assert llm._payload('fast', [], .3, False)[1]['model'] == 'legacy'
        assert llm.available('reasoning') is False
