from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes


@pytest.mark.parametrize('error,status', [(ValueError('private detail'), 422),
    (OSError('private path'), 422), (routes.settings_store.RevisionConflict('conflict'), 409)])
def test_legacy_settings_write_returns_safe_error(error, status):
    app = FastAPI()
    app.include_router(routes.router)
    with patch.object(routes.settings_store, 'write', side_effect=error), TestClient(app) as client:
        response = client.put('/api/settings', json={'model_profiles': []})
    assert response.status_code == status
    assert 'private' not in response.text
