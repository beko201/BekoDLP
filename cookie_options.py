"""Cookie source references shared by preferences, frozen tasks and the CLI."""
import os

COOKIE_BROWSERS = ('chrome', 'edge', 'firefox', 'brave', 'opera', 'vivaldi', 'chromium', 'whale', 'safari')
COOKIE_DEFAULTS = dict(cookie_mode='none', cookie_file='', cookie_browser='chrome', cookie_profile='')
BROWSER_COOKIE_COPY_HELP = (
    'The browser cookie database could not be read. Close the browser completely, '
    'including background processes, and retry. If it still fails, choose Cookie file '
    'and select a Netscape cookies.txt file.')


def cookie_error_guidance(message):
    """Recognize cookie-copy failures without changing authentication settings."""
    message = str(message).lower()
    if 'could not copy' in message and 'cookie database' in message:
        return BROWSER_COOKIE_COPY_HELP
    return ''


def cookie_selection(data=None):
    data = data or {}
    return {key: data.get(key, default) for key, default in COOKIE_DEFAULTS.items()}


def validate_cookie_options(data, *, check_file=False):
    values = cookie_selection(data)
    if any(not isinstance(value, str) or len(value) > 32768 or any(c in value for c in '\x00\r\n')
           for value in values.values()):
        raise ValueError('Invalid cookie settings.')
    if values['cookie_mode'] not in ('none', 'file', 'browser'):
        raise ValueError('Unknown cookie source.')
    if values['cookie_browser'] not in COOKIE_BROWSERS:
        raise ValueError('Unsupported cookie browser.')
    if values['cookie_mode'] == 'file':
        if not values['cookie_file'].strip():
            raise ValueError('Choose a Netscape cookies.txt file first.')
        if check_file and not os.path.isfile(values['cookie_file']):
            raise ValueError('The selected cookie file does not exist.')
    return values


def cookie_kwargs(data=None):
    """Keep the no-cookie call contract unchanged for existing integrations."""
    values = cookie_selection(data)
    return {'cookie_options': values} if values['cookie_mode'] != 'none' else {}
