import time
from urllib.parse import urlparse, parse_qs

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import db
import blueprints.oidc as oidc

ISSUER = 'https://auth.example.test/application/o/blaeu/'
CLIENT_ID = 'blaeu-client'

DISCOVERY = {
    'issuer': ISSUER,
    'authorization_endpoint': 'https://auth.example.test/application/o/authorize/',
    'token_endpoint': 'https://auth.example.test/application/o/token/',
    'userinfo_endpoint': 'https://auth.example.test/application/o/userinfo/',
    'jwks_uri': 'https://auth.example.test/application/o/blaeu/jwks/',
    'id_token_signing_alg_values_supported': ['RS256'],
    'token_endpoint_auth_methods_supported': ['client_secret_basic', 'client_secret_post'],
}

SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.ok = status < 400
        self.text = str(payload)

    def json(self):
        return self.payload

    def raise_for_status(self):
        if not self.ok:
            raise oidc.requests.HTTPError(self.status_code)


class FakeProvider:
    """Stands in for authentik: discovery, token endpoint and signing keys."""

    def __init__(self, monkeypatch):
        self.claims = {}
        self.userinfo = {}
        self.token_requests = []
        self.signing_key = SIGNING_KEY
        monkeypatch.setattr(oidc.requests, 'get', self.get)
        monkeypatch.setattr(oidc.requests, 'post', self.post)
        monkeypatch.setattr(oidc, '_jwks_client', lambda uri: self)
        oidc._discovery_cache.clear()

    def get(self, url, **kwargs):
        if url.endswith('/.well-known/openid-configuration'):
            return FakeResponse(DISCOVERY)
        if url == DISCOVERY['userinfo_endpoint']:
            return FakeResponse(self.userinfo)
        return FakeResponse({}, 404)

    def post(self, url, data=None, auth=None, **kwargs):
        self.token_requests.append({'data': data, 'auth': auth})
        now = int(time.time())
        claims = {'iss': ISSUER, 'aud': CLIENT_ID, 'iat': now, 'exp': now + 300,
                  'nonce': self.nonce, **self.claims}
        id_token = jwt.encode(claims, self.signing_key, algorithm='RS256')
        return FakeResponse({'id_token': id_token, 'access_token': 'access', 'token_type': 'Bearer'})

    def get_signing_key_from_jwt(self, token):
        return type('Key', (), {'key': SIGNING_KEY.public_key()})()

    def login(self, client, sub, username, start='/api/auth/oidc/login', **extra):
        """Runs the whole flow in the browser and returns the final redirect."""
        res = client.get(start)
        assert res.status_code == 302
        params = parse_qs(urlparse(res.headers['Location']).query)
        self.nonce = params['nonce'][0]
        self.claims = {'sub': sub, 'preferred_username': username, **extra}
        return client.get(f"/api/auth/oidc/callback?code=abc&state={params['state'][0]}")


@pytest.fixture
def provider(client, monkeypatch):
    monkeypatch.setenv('BLAEU_OIDC_ISSUER', ISSUER)
    monkeypatch.setenv('BLAEU_OIDC_CLIENT_ID', CLIENT_ID)
    monkeypatch.setenv('BLAEU_OIDC_CLIENT_SECRET', 'secret')
    monkeypatch.setenv('BLAEU_OIDC_PROVIDER_NAME', 'authentik')
    monkeypatch.setenv('BLAEU_ALLOW_REGISTRATION', 'false')
    return FakeProvider(monkeypatch)


def register(client, username, password='Password123'):
    client.post('/api/auth/logout')
    res = client.post('/api/auth/register', json={'username': username, 'password': password})
    assert res.status_code == 201
    return res.get_json()['user']['id']


def status(client):
    return client.get('/api/auth/status').get_json()


def sso_error(res):
    return parse_qs(urlparse(res.headers['Location']).query).get('sso_error', [None])[0]


def test_status_without_oidc(client):
    data = status(client)
    assert data['sso']['enabled'] is False
    assert data['sso']['password_login'] is True
    assert client.get('/api/auth/oidc/login').status_code == 404


def test_login_redirects_with_pkce(client, provider):
    assert status(client)['sso'] == {'enabled': True, 'provider_name': 'authentik', 'password_login': True}
    res = client.get('/api/auth/oidc/login')
    url = urlparse(res.headers['Location'])
    params = parse_qs(url.query)
    assert url.geturl().startswith(DISCOVERY['authorization_endpoint'])
    assert params['client_id'] == [CLIENT_ID]
    assert params['code_challenge_method'] == ['S256']
    assert params['redirect_uri'] == ['http://localhost/api/auth/oidc/callback']
    assert 'openid' in params['scope'][0]


def test_first_login_on_empty_instance_creates_admin(client, provider):
    res = provider.login(client, 'sub-1', 'jan')
    assert res.headers['Location'] == '/'
    data = status(client)
    assert data['logged_in'] is True
    assert data['user']['username'] == 'jan'
    assert data['user']['is_admin'] == 1
    assert data['user']['sso'] == {'linked': True, 'identity': 'jan', 'has_password': False}
    assert provider.token_requests[0]['auth'] == (CLIENT_ID, 'secret')
    assert provider.token_requests[0]['data']['code_verifier']

    # The next login finds the linked identity, even after a rename at the provider
    client.post('/api/auth/logout')
    provider.login(client, 'sub-1', 'jan-renamed')
    assert status(client)['user']['username'] == 'jan'


def test_unknown_identity_is_rejected_without_auto_create(client, provider):
    register(client, 'admin')
    client.post('/api/auth/logout')
    res = provider.login(client, 'sub-2', 'stranger')
    assert 'no Blaeu account' in sso_error(res)
    assert status(client)['logged_in'] is False


def test_auto_create(client, provider, monkeypatch):
    monkeypatch.setenv('BLAEU_OIDC_AUTO_CREATE', 'true')
    register(client, 'admin')
    client.post('/api/auth/logout')
    provider.login(client, 'sub-2', 'newbie')
    user = status(client)['user']
    assert user['username'] == 'newbie'
    assert user['is_admin'] == 0

    # Never takes over an existing account by name
    client.post('/api/auth/logout')
    res = provider.login(client, 'sub-3', 'admin')
    assert 'already exists' in sso_error(res)


def test_link_from_settings_keeps_existing_account(authed_client, provider):
    user_id = status(authed_client)['user']['id']
    res = authed_client.post('/api/auth/oidc/link')
    params = parse_qs(urlparse(res.get_json()['url']).query)
    provider.nonce = params['nonce'][0]
    provider.claims = {'sub': 'sub-9', 'preferred_username': 'jan.s'}
    res = authed_client.get(f"/api/auth/oidc/callback?code=abc&state={params['state'][0]}")
    assert res.headers['Location'] == '/'
    assert status(authed_client)['user']['sso']['identity'] == 'jan.s'

    authed_client.post('/api/auth/logout')
    provider.login(authed_client, 'sub-9', 'jan.s')
    data = status(authed_client)
    assert data['user']['id'] == user_id
    assert data['user']['username'] == 'test_user'

    # Password login keeps working, and the link can be undone
    assert authed_client.delete('/api/auth/oidc/link').status_code == 200
    assert status(authed_client)['user']['sso']['linked'] is False


def test_identity_cannot_link_to_two_accounts(client, provider):
    provider.login(client, 'sub-1', 'jan')  # creates the first account, linked to sub-1
    other_id = db.add_user('other', 'x', 0)
    with client.session_transaction() as sess:
        sess['user_id'] = other_id
    res = client.post('/api/auth/oidc/link')
    params = parse_qs(urlparse(res.get_json()['url']).query)
    provider.nonce = params['nonce'][0]
    provider.claims = {'sub': 'sub-1', 'preferred_username': 'jan'}
    res = client.get(f"/api/auth/oidc/callback?code=abc&state={params['state'][0]}")
    assert 'already linked to another Blaeu account' in sso_error(res)
    assert db.get_identity(ISSUER, 'sub-1')['user_id'] != other_id


def test_admin_mapping_links_at_first_login(client, provider):
    register(client, 'admin')
    user_id = db.add_user('marieke', 'x', 0)
    res = client.put(f'/api/auth/users/{user_id}/sso', json={'oidc_username': 'Marieke.D'})
    assert res.status_code == 200
    users = {u['username']: u for u in client.get('/api/auth/users').get_json()}
    assert users['marieke']['oidc_username'] == 'Marieke.D'

    client.post('/api/auth/logout')
    provider.login(client, 'sub-m', 'marieke.d')
    assert status(client)['user']['id'] == user_id

    users = {u['username']: u for u in db.get_users()}
    assert users['marieke']['sso_identity'] == 'marieke.d'
    assert users['marieke']['oidc_username'] is None


def test_admin_mapping_requires_admin(client, provider, monkeypatch):
    monkeypatch.setenv('BLAEU_ALLOW_REGISTRATION', 'true')
    admin_id = register(client, 'admin')
    register(client, 'regular')
    res = client.put(f'/api/auth/users/{admin_id}/sso', json={'oidc_username': 'evil'})
    assert res.status_code == 403


def test_admin_mapping_must_be_unique(client, provider):
    register(client, 'admin')
    a = db.add_user('a', 'x', 0)
    b = db.add_user('b', 'x', 0)
    assert client.put(f'/api/auth/users/{a}/sso', json={'oidc_username': 'same'}).status_code == 200
    assert client.put(f'/api/auth/users/{b}/sso', json={'oidc_username': 'same'}).status_code == 400


def test_match_username(client, provider, monkeypatch):
    user_id = register(client, 'jan')
    client.post('/api/auth/logout')
    res = provider.login(client, 'sub-1', 'jan')
    assert sso_error(res)

    monkeypatch.setenv('BLAEU_OIDC_MATCH_USERNAME', 'true')
    provider.login(client, 'sub-1', 'jan')
    assert status(client)['user']['id'] == user_id

    # A second identity with the same name can't claim the linked account
    client.post('/api/auth/logout')
    res = provider.login(client, 'sub-other', 'jan')
    assert 'already linked' in sso_error(res)


def test_unverified_email_is_not_used_to_match(client, provider, monkeypatch):
    monkeypatch.setenv('BLAEU_OIDC_USERNAME_CLAIM', 'email')
    monkeypatch.setenv('BLAEU_OIDC_MATCH_USERNAME', 'true')
    register(client, 'jan@example.com')
    client.post('/api/auth/logout')
    res = provider.login(client, 'sub-1', None, email='jan@example.com', email_verified=False)
    assert sso_error(res)
    provider.login(client, 'sub-1', None, email='jan@example.com', email_verified=True)
    assert status(client)['user']['username'] == 'jan@example.com'


def test_rejects_bad_state_and_bad_signature(client, provider):
    client.get('/api/auth/oidc/login')
    res = client.get('/api/auth/oidc/callback?code=abc&state=forged')
    assert 'could not be verified' in sso_error(res)

    provider.signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    res = provider.login(client, 'sub-1', 'jan')
    assert 'could not be verified' in sso_error(res)
    assert status(client)['logged_in'] is False


def test_rejects_wrong_nonce(client, provider):
    res = client.get('/api/auth/oidc/login')
    params = parse_qs(urlparse(res.headers['Location']).query)
    provider.nonce = 'replayed'
    provider.claims = {'sub': 'sub-1', 'preferred_username': 'jan'}
    res = client.get(f"/api/auth/oidc/callback?code=abc&state={params['state'][0]}")
    assert sso_error(res)
    assert status(client)['logged_in'] is False


def test_disable_password_login(client, provider, monkeypatch):
    register(client, 'jan')
    monkeypatch.setenv('BLAEU_OIDC_DISABLE_PASSWORD_LOGIN', 'true')
    client.post('/api/auth/logout')
    assert status(client)['sso']['password_login'] is False
    res = client.post('/api/auth/login', json={'username': 'jan', 'password': 'Password123'})
    assert res.status_code == 403


def test_sso_only_account_cannot_password_login_or_unlink(client, provider):
    provider.login(client, 'sub-1', 'jan')
    assert client.delete('/api/auth/oidc/link').status_code == 400
    client.post('/api/auth/logout')
    res = client.post('/api/auth/login', json={'username': 'jan', 'password': 'anything1A'})
    assert res.status_code == 401


def test_deleting_user_removes_identity(client, provider, monkeypatch):
    monkeypatch.setenv('BLAEU_OIDC_AUTO_CREATE', 'true')
    register(client, 'admin')
    client.post('/api/auth/logout')
    provider.login(client, 'sub-2', 'leaver')
    leaver_id = status(client)['user']['id']
    client.post('/api/auth/logout')
    client.post('/api/auth/login', json={'username': 'admin', 'password': 'Password123'})
    assert client.delete(f'/api/auth/users/{leaver_id}').status_code == 200
    assert db.get_identity(ISSUER, 'sub-2') is None
