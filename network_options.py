"""Frozen proxy and per-task media transfer limits."""
from urllib.parse import urlsplit

NETWORK_DEFAULTS = dict(proxy_mode='system', proxy_url='', download_rate='')
MAX_RATE = 1024 ** 4


def network_selection(data=None):
    data = data or {}
    return {key: data.get(key, default) for key, default in NETWORK_DEFAULTS.items()}


def validate_network_options(data):
    selected = network_selection(data)
    if any(not isinstance(value, str) or len(value) > 32768 or any(c in value for c in '\x00\r\n')
           for value in selected.values()):
        raise ValueError('Invalid network settings.')
    if selected['proxy_mode'] not in ('system', 'direct', 'custom'):
        raise ValueError('Unknown proxy mode.')
    if selected['proxy_mode'] == 'custom':
        try:
            url = urlsplit(selected['proxy_url'])
            valid = (url.scheme in ('http', 'https', 'socks4', 'socks4a', 'socks5', 'socks5h') and url.hostname
                     and url.port is not None and 1 <= url.port <= 65535
                     and not url.query and not url.fragment and url.path in ('', '/'))
        except ValueError:
            valid = False
        if not valid or any(c.isspace() for c in selected['proxy_url']):
            raise ValueError('Enter a proxy URL with a scheme, host and port, such as http://127.0.0.1:8080.')
    rate = selected['download_rate']
    if rate and (not rate.isascii() or not rate.isdecimal() or len(rate) > 13 or not 1 <= int(rate) <= MAX_RATE):
        raise ValueError('Invalid download speed limit.')
    return selected


def network_kwargs(data=None):
    selected = network_selection(data)
    return {'network_options': selected} if selected != NETWORK_DEFAULTS else {}
