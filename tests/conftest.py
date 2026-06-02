import os
import tempfile
import pytest
import shutil
from app import app
import db

@pytest.fixture
def clean_env(monkeypatch):
    db_fd, temp_db_path = tempfile.mkstemp()
    temp_gpx_dir = tempfile.mkdtemp()
    
    # Configure app env vars for testing
    monkeypatch.setenv("DATA_DIR", temp_gpx_dir)
    monkeypatch.setenv("BLAEU_ALLOW_REGISTRATION", "true")
    monkeypatch.setattr("db.DB_PATH", temp_db_path)
    monkeypatch.setattr("db.DATA_DIR", temp_gpx_dir)
    monkeypatch.setattr("app.DATA_DIR", temp_gpx_dir)
    monkeypatch.setattr("app.GPX_STORE_DIR", os.path.join(temp_gpx_dir, 'gpx'))
    monkeypatch.setattr("app.TILES_CACHE_DIR", os.path.join(temp_gpx_dir, 'tiles_cache'))
    
    # Re-initialize the test database
    db.init_db()
    
    app.config.update({
        'TESTING': True,
        'PROPAGATE_EXCEPTIONS': True,
        'SECRET_KEY': 'test-secret-key'
    })
    
    yield app
    
    os.close(db_fd)
    os.unlink(temp_db_path)
    shutil.rmtree(temp_gpx_dir, ignore_errors=True)

@pytest.fixture
def client(clean_env):
    with clean_env.test_client() as c:
        yield c

@pytest.fixture
def authed_client(client):
    client.post('/api/auth/register', json={
        'username': 'test_user',
        'password': 'Password123'
    })
    return client
