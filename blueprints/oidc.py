"""
OpenID Connect single sign-on (e.g. authentik, Keycloak, Authelia).

Uses the authorization code flow with PKCE. An identity provider account is linked to a
local account by (issuer, subject), so existing accounts keep all their data. A login
from an identity that isn't linked yet is matched in this order:

1. The logged-in user who started "Link single sign-on" in Settings.
2. An account an admin mapped to the provider username (Settings > Admin).
3. An account with the same username, when BLAEU_OIDC_MATCH_USERNAME is true.
4. A new account, when BLAEU_OIDC_AUTO_CREATE is true or no accounts exist yet.
"""
import os
import time
import base64
import hashlib
import secrets
import logging
import threading
from urllib.parse import urlencode

import jwt
import requests
from flask import Blueprint, request, jsonify, session, redirect, url_for

from db import (
    count_users, get_user_by_id, get_user_by_username, get_user_by_oidc_username, add_user,
    backfill_ownerless_data, get_identity, get_user_identity, link_identity, touch_identity,
    unlink_identities, set_user_oidc_username
)
from utils import rate_limit, get_current_user_id

logger = logging.getLogger('blaeu.oidc')
oidc_bp = Blueprint('oidc', __name__)

DISCOVERY_TTL = 3600
HTTP_TIMEOUT = 10
FLOW_TTL = 600  # seconds a login may take at the identity provider


def _env_flag(name, default='false'):
    return os.getenv(name, default).lower() == 'true'


def oidc_config():
    """The OIDC settings from the environment, or None when single sign-on is off."""
    issuer = os.getenv('BLAEU_OIDC_ISSUER', '').strip()
    client_id = os.getenv('BLAEU_OIDC_CLIENT_ID', '').strip()
    if not issuer or not client_id:
        return None
    return {
        'issuer': issuer,
        'client_id': client_id,
        'client_secret': os.getenv('BLAEU_OIDC_CLIENT_SECRET', ''),
        'provider_name': os.getenv('BLAEU_OIDC_PROVIDER_NAME', '').strip() or 'Single Sign-On',
        'scopes': os.getenv('BLAEU_OIDC_SCOPES', '').strip() or 'openid profile email',
        'username_claim': os.getenv('BLAEU_OIDC_USERNAME_CLAIM', '').strip() or 'preferred_username',
        'redirect_uri': os.getenv('BLAEU_OIDC_REDIRECT_URI', '').strip() or None,
        'auto_create': _env_flag('BLAEU_OIDC_AUTO_CREATE'),
        'match_username': _env_flag('BLAEU_OIDC_MATCH_USERNAME'),
    }


def password_login_enabled():
    return oidc_config() is None or not _env_flag('BLAEU_OIDC_DISABLE_PASSWORD_LOGIN')


def sso_status():
    """What the login page needs to know about single sign-on."""
    config = oidc_config()
    return {
        'enabled': config is not None,
        'provider_name': config['provider_name'] if config else None,
        'password_login': password_login_enabled(),
    }


def user_sso_info(user):
    """The SSO link state of an account, for /api/auth/status."""
    config = oidc_config()
    if not config:
        return None
    identity = get_user_identity(user['id'])
    return {
        'linked': identity is not None,
        'identity': (identity.get('username') or identity.get('email')) if identity else None,
        'has_password': bool(user.get('password_hash')),
    }


# ----------------------------------------------------
# Provider metadata
# ----------------------------------------------------

_discovery_cache = {}
_jwks_clients = {}
_cache_lock = threading.Lock()


def _issuer(config):
    """The issuer as the provider states it (authentik's ends with a slash)."""
    return discovery(config).get('issuer') or config['issuer']


def discovery(config):
    url = config['issuer'].rstrip('/') + '/.well-known/openid-configuration'
    with _cache_lock:
        cached = _discovery_cache.get(url)
        if cached and cached[0] > time.time():
            return cached[1]
    res = requests.get(url, timeout=HTTP_TIMEOUT)
    res.raise_for_status()
    metadata = res.json()
    for key in ('authorization_endpoint', 'token_endpoint', 'jwks_uri'):
        if not metadata.get(key):
            raise ValueError(f"OIDC discovery document has no {key}")
    with _cache_lock:
        _discovery_cache[url] = (time.time() + DISCOVERY_TTL, metadata)
    return metadata


def _jwks_client(jwks_uri):
    with _cache_lock:
        client = _jwks_clients.get(jwks_uri)
        if client is None:
            client = jwt.PyJWKClient(jwks_uri, cache_keys=True, lifespan=DISCOVERY_TTL, timeout=HTTP_TIMEOUT)
            _jwks_clients[jwks_uri] = client
        return client


def _redirect_uri(config):
    return config['redirect_uri'] or url_for('oidc.sso_callback', _external=True)


def _pkce_challenge(verifier):
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')


def _authorize_url(config, link_user_id=None):
    """Starts a login at the provider, remembering the flow in the session."""
    metadata = discovery(config)
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    session['oidc_flow'] = {
        'state': state,
        'nonce': nonce,
        'verifier': verifier,
        'link_user_id': link_user_id,
        'expires': int(time.time()) + FLOW_TTL,
    }
    params = {
        'response_type': 'code',
        'client_id': config['client_id'],
        'redirect_uri': _redirect_uri(config),
        'scope': config['scopes'],
        'state': state,
        'nonce': nonce,
        'code_challenge': _pkce_challenge(verifier),
        'code_challenge_method': 'S256',
    }
    return metadata['authorization_endpoint'] + ('&' if '?' in metadata['authorization_endpoint'] else '?') + urlencode(params)


def _exchange_code(config, code, verifier):
    """Trades the authorization code for tokens at the token endpoint."""
    metadata = discovery(config)
    data = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': _redirect_uri(config),
        'code_verifier': verifier,
    }
    auth = None
    methods = metadata.get('token_endpoint_auth_methods_supported') or ['client_secret_basic']
    if config['client_secret'] and 'client_secret_basic' in methods:
        auth = (config['client_id'], config['client_secret'])
    else:
        data['client_id'] = config['client_id']
        if config['client_secret']:
            data['client_secret'] = config['client_secret']
    res = requests.post(metadata['token_endpoint'], data=data, auth=auth, timeout=HTTP_TIMEOUT,
                        headers={'Accept': 'application/json'})
    if not res.ok:
        logger.warning("OIDC token exchange failed (%s): %s", res.status_code, res.text[:300])
        raise ValueError("The identity provider rejected the login")
    return res.json()


def _verify_id_token(config, id_token, nonce):
    metadata = discovery(config)
    signing_key = _jwks_client(metadata['jwks_uri']).get_signing_key_from_jwt(id_token)
    algorithms = metadata.get('id_token_signing_alg_values_supported') or ['RS256']
    claims = jwt.decode(
        id_token,
        signing_key.key,
        algorithms=[alg for alg in algorithms if alg != 'none'],
        audience=config['client_id'],
        issuer=_issuer(config),
        options={'require': ['exp', 'iat', 'iss', 'aud', 'sub']},
        leeway=60,
    )
    if claims.get('nonce') != nonce:
        raise jwt.InvalidTokenError("nonce mismatch")
    return claims


def _userinfo(config, access_token):
    endpoint = discovery(config).get('userinfo_endpoint')
    if not endpoint or not access_token:
        return {}
    try:
        res = requests.get(endpoint, headers={'Authorization': f'Bearer {access_token}'}, timeout=HTTP_TIMEOUT)
        res.raise_for_status()
        return res.json()
    except (requests.RequestException, ValueError):
        logger.warning("OIDC userinfo request failed", exc_info=True)
        return {}


def _provider_username(config, claims):
    """The value used to match and name accounts; an email only counts when verified."""
    claim = config['username_claim']
    value = claims.get(claim)
    if claim == 'email' and claims.get('email_verified') is False:
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


# ----------------------------------------------------
# Routes
# ----------------------------------------------------

def _fail(message):
    session.pop('oidc_flow', None)
    return redirect('/?' + urlencode({'sso_error': message}))


def _login(user_id):
    session.pop('oidc_flow', None)
    session.pop('csrf_token', None)
    session['user_id'] = user_id
    return redirect('/')


@oidc_bp.route('/api/auth/oidc/login', methods=['GET'])
@rate_limit(limit=10, period=60)
def sso_login():
    config = oidc_config()
    if not config:
        return jsonify({'error': 'Single sign-on is not configured'}), 404
    try:
        return redirect(_authorize_url(config))
    except (requests.RequestException, ValueError):
        logger.exception("Could not reach the OIDC provider")
        return _fail(f"Could not reach {config['provider_name']}. Please try again later.")


@oidc_bp.route('/api/auth/oidc/link', methods=['POST'])
def start_link():
    """Links the logged-in account to an identity: returns the URL to continue at the provider."""
    config = oidc_config()
    if not config:
        return jsonify({'error': 'Single sign-on is not configured'}), 404
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
    try:
        return jsonify({'url': _authorize_url(config, link_user_id=user_id)})
    except (requests.RequestException, ValueError):
        logger.exception("Could not reach the OIDC provider")
        return jsonify({'error': f"Could not reach {config['provider_name']}"}), 502


@oidc_bp.route('/api/auth/oidc/link', methods=['DELETE'])
def unlink_self():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
    user = get_user_by_id(user_id)
    if not user.get('password_hash') or not password_login_enabled():
        return jsonify({'error': 'Single sign-on is the only way to log in to this account, so it cannot be unlinked.'}), 400
    unlink_identities(user_id)
    return jsonify({'success': True})


@oidc_bp.route('/api/auth/oidc/callback', methods=['GET'])
@rate_limit(limit=10, period=60)
def sso_callback():
    config = oidc_config()
    if not config:
        return redirect('/')
    provider = config['provider_name']

    flow = session.get('oidc_flow')
    if not flow or flow.get('expires', 0) < time.time():
        return _fail("The login took too long or was started elsewhere. Please try again.")
    if not request.args.get('state') or not secrets.compare_digest(request.args['state'], flow['state']):
        return _fail("The login could not be verified. Please try again.")
    if request.args.get('error'):
        logger.info("OIDC provider returned an error: %s", request.args.get('error'))
        return _fail(f"{provider} did not complete the login.")
    code = request.args.get('code')
    if not code:
        return _fail(f"{provider} did not complete the login.")

    try:
        tokens = _exchange_code(config, code, flow['verifier'])
        claims = _verify_id_token(config, tokens.get('id_token') or '', flow['nonce'])
    except (requests.RequestException, ValueError, jwt.PyJWTError):
        logger.warning("OIDC login failed", exc_info=True)
        return _fail(f"The login with {provider} could not be verified. Please try again.")

    if config['username_claim'] not in claims or 'email' not in claims:
        claims = {**_userinfo(config, tokens.get('access_token')), **claims}

    issuer = claims['iss']
    subject = claims['sub']
    username = _provider_username(config, claims)
    email = claims.get('email') if claims.get('email_verified') is not False else None
    identity = get_identity(issuer, subject)

    # Linking from Settings: attach the identity to the account that started it
    link_user_id = flow.get('link_user_id')
    if link_user_id:
        if get_current_user_id() != link_user_id or not get_user_by_id(link_user_id):
            return _fail("Your session changed while linking. Please log in and try again.")
        if identity and identity['user_id'] != link_user_id:
            return _fail(f"This {provider} account is already linked to another Blaeu account.")
        if not identity:
            try:
                link_identity(link_user_id, issuer, subject, username, email)
            except ValueError:
                return _fail(f"This Blaeu account is already linked to another {provider} account.")
            logger.info("Linked OIDC identity %s to user %s", subject, link_user_id)
        return _login(link_user_id)

    if identity:
        touch_identity(identity['id'], username, email)
        return _login(identity['user_id'])

    # First login with this identity: find the existing account it belongs to
    user = None
    if username:
        user = get_user_by_oidc_username(username)
        if not user and config['match_username']:
            user = get_user_by_username(username)
    if user:
        if get_user_identity(user['id'], issuer):
            return _fail(f"The Blaeu account for {username} is already linked to another {provider} account.")
        try:
            link_identity(user['id'], issuer, subject, username, email)
        except ValueError:
            return _fail("This account could not be linked. Please try again.")
        logger.info("Linked OIDC identity %s to existing user %s", subject, user['id'])
        return _login(user['id'])

    no_users = count_users() == 0
    if not (config['auto_create'] or no_users):
        return _fail(f"There is no Blaeu account for this {provider} account yet. "
                     "Log in with your password and link it in Settings, or ask an administrator to map it.")
    if not username:
        return _fail(f"{provider} did not share a username ({config['username_claim']}).")
    if get_user_by_username(username):
        return _fail(f"A Blaeu account named {username} already exists. "
                     "Log in with its password and link it in Settings, or ask an administrator to map it.")
    try:
        user_id = add_user(username, '', 1 if no_users else 0)
        link_identity(user_id, issuer, subject, username, email)
    except ValueError:
        return _fail("The account could not be created. Please try again.")
    if no_users:
        backfill_ownerless_data(user_id)
    logger.info("Created user %s for OIDC identity %s", user_id, subject)
    return _login(user_id)


@oidc_bp.route('/api/auth/users/<int:target_user_id>/sso', methods=['PUT'])
def admin_map_user(target_user_id):
    """Admins map an account to a provider username (claimed at its first SSO login) or unlink it."""
    admin = get_user_by_id(session.get('user_id')) if session.get('user_id') else None
    if not admin:
        return jsonify({'error': 'Unauthorized'}), 401
    if not admin['is_admin']:
        return jsonify({'error': 'Forbidden. Admin privileges required.'}), 403
    if not get_user_by_id(target_user_id):
        return jsonify({'error': 'User not found'}), 404

    data = request.json or {}
    oidc_username = data.get('oidc_username')
    if oidc_username is not None and not isinstance(oidc_username, str):
        return jsonify({'error': 'oidc_username must be text'}), 400
    oidc_username = (oidc_username or '').strip() or None
    if oidc_username:
        other = get_user_by_oidc_username(oidc_username)
        if other and other['id'] != target_user_id:
            return jsonify({'error': f"{oidc_username} is already mapped to {other['username']}"}), 400

    if data.get('unlink'):
        unlink_identities(target_user_id)
    set_user_oidc_username(target_user_id, oidc_username)
    return jsonify({'success': True})
