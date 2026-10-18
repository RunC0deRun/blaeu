"""
LLM endpoint settings. Admins manage them in the UI (stored in app_settings); environment
variables provide defaults, and BLAEU_LLM_API_KEY always wins over a stored key so a
secret can stay out of the database.
"""
import ipaddress
import os
from urllib.parse import urlparse

from db import get_app_settings, set_app_settings

PREFIX = 'llm.'
DEFAULT_CONTEXT_WINDOW = 8192
MIN_CONTEXT_WINDOW = 2048
MAX_CONTEXT_WINDOW = 1_000_000
DEFAULT_TEMPERATURE = 0.4
DEFAULT_MAX_ANSWER_TOKENS = 1024

# Host names that stay on this machine or local network
LOCAL_SUFFIXES = ('.local', '.lan', '.home', '.internal', '.home.arpa', '.localhost')


class SettingsError(ValueError):
    pass


def _env_bool(name, default=False):
    value = os.getenv(name)
    return default if value is None else value.lower() == 'true'


def get_llm_config():
    """The effective configuration, including the API key. Never send this to a browser."""
    stored = get_app_settings(PREFIX)

    def pick(key, env, default=None):
        value = stored.get(PREFIX + key)
        return value if value not in (None, '') else os.getenv(env, default)

    env_key = os.getenv('BLAEU_LLM_API_KEY')
    try:
        context_window = int(pick('context_window', 'BLAEU_LLM_CONTEXT_WINDOW', DEFAULT_CONTEXT_WINDOW))
    except ValueError:
        context_window = DEFAULT_CONTEXT_WINDOW
    try:
        temperature = float(pick('temperature', 'BLAEU_LLM_TEMPERATURE', DEFAULT_TEMPERATURE))
    except ValueError:
        temperature = DEFAULT_TEMPERATURE
    enabled_value = stored.get(PREFIX + 'enabled')
    return {
        'enabled': enabled_value == 'true' if enabled_value is not None else _env_bool('BLAEU_LLM_ENABLED'),
        'base_url': (pick('base_url', 'BLAEU_LLM_BASE_URL', '') or '').rstrip('/'),
        'model': pick('model', 'BLAEU_LLM_MODEL', '') or '',
        'api_key': env_key or stored.get(PREFIX + 'api_key') or '',
        'api_key_from_env': bool(env_key),
        'context_window': context_window,
        'temperature': temperature,
        'max_answer_tokens': DEFAULT_MAX_ANSWER_TOKENS,
    }


def public_llm_config(config=None):
    """The configuration as shown to admins: the API key is replaced by whether one is set."""
    config = dict(config or get_llm_config())
    api_key = config.pop('api_key')
    config['api_key_set'] = bool(api_key)
    config['is_external'] = is_external_endpoint(config['base_url']) if config['base_url'] else False
    return config


def is_configured(config):
    return bool(config['enabled'] and config['base_url'] and config['model'])


def is_external_endpoint(base_url):
    """
    True when the endpoint is likely outside this machine and local network, meaning
    training and health data would leave the server. Unknown cases count as external.
    """
    host = (urlparse(base_url).hostname or '').lower()
    if not host:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local)
    except ValueError:
        pass
    if host == 'localhost' or '.' not in host:  # e.g. a Docker service name like "ollama"
        return False
    return not host.endswith(LOCAL_SUFFIXES)


def validate_base_url(base_url):
    parsed = urlparse(base_url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise SettingsError('The endpoint URL must start with http:// or https:// and include a host.')
    return base_url.rstrip('/')


def parse_llm_settings(data):
    """
    Validates admin input and returns the changes as {field: value}. Only keys present in
    data change; an empty api_key means "keep the stored key", clear_api_key removes it
    (value None). Raises SettingsError with a message for the admin.
    """
    changes = {}
    if 'enabled' in data:
        if not isinstance(data['enabled'], bool):
            raise SettingsError('enabled must be true or false.')
        changes['enabled'] = data['enabled']
    if 'base_url' in data:
        value = (data['base_url'] or '').strip()
        changes['base_url'] = validate_base_url(value) if value else ''
    if 'model' in data:
        value = (data['model'] or '').strip()
        if len(value) > 200:
            raise SettingsError('The model name is too long.')
        changes['model'] = value
    if 'context_window' in data:
        value = data['context_window']
        if not isinstance(value, int) or isinstance(value, bool) \
                or not MIN_CONTEXT_WINDOW <= value <= MAX_CONTEXT_WINDOW:
            raise SettingsError(
                f'The context window must be a whole number between {MIN_CONTEXT_WINDOW} and {MAX_CONTEXT_WINDOW}.'
            )
        changes['context_window'] = value
    if 'temperature' in data:
        value = data['temperature']
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 2:
            raise SettingsError('The temperature must be a number between 0 and 2.')
        changes['temperature'] = float(value)
    if data.get('clear_api_key'):
        changes['api_key'] = None
    elif data.get('api_key'):
        if not isinstance(data['api_key'], str) or len(data['api_key']) > 500:
            raise SettingsError('The API key is invalid.')
        changes['api_key'] = data['api_key'].strip()
    return changes


def preview_llm_config(data):
    """The effective configuration with unsaved changes applied, e.g. to test a form."""
    config = get_llm_config()
    changes = parse_llm_settings(data)
    if 'api_key' in changes and not config['api_key_from_env']:
        config['api_key'] = changes.pop('api_key') or ''
    changes.pop('api_key', None)
    config.update(changes)
    return config


def save_llm_settings(data):
    changes = parse_llm_settings(data)
    updates = {}
    for field, value in changes.items():
        if field == 'enabled':
            value = 'true' if value else 'false'
        updates[PREFIX + field] = None if value in (None, '') else value
    set_app_settings(updates)
    return get_llm_config()
