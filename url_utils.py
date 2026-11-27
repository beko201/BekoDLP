"""URL normalization without network or downloader dependencies."""
import re
from urllib.parse import parse_qs, urlsplit


def normalize_url(url: str) -> str:
    """Canonicalize recognized YouTube video URLs while preserving other URLs."""
    if not url:
        return url
    url = url.strip()
    try:
        # yt-dlp also accepts links pasted without a scheme.
        parsed = urlsplit(url if '://' in url or url.startswith('//') else '//' + url)
        if parsed.scheme not in ('', 'http', 'https') or parsed.username is not None:
            return url
        host = (parsed.hostname or '').lower()
        if parsed.port is not None and parsed.port not in (80, 443):
            return url
    except ValueError:
        return url  # Leave malformed input to the extractor's error reporting.
    youtube_hosts = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'}
    path = parsed.path.rstrip('/')
    video_id = None
    if host in {'youtu.be', 'www.youtu.be'}:
        video_id = path.removeprefix('/')
    elif host in youtube_hosts:
        if path == '/watch':
            video_id = (parse_qs(parsed.query).get('v') or [None])[0]
        else:
            match = re.fullmatch(r'/(?:shorts|live|embed)/([A-Za-z0-9_-]+)', path)
            if match:
                video_id = match.group(1)
    elif host in {'youtube-nocookie.com', 'www.youtube-nocookie.com'}:
        match = re.fullmatch(r'/embed/([A-Za-z0-9_-]+)', path)
        if match:
            video_id = match.group(1)
    if video_id and re.fullmatch(r'[A-Za-z0-9_-]+', video_id):
        return f"https://www.youtube.com/watch?v={video_id}"
    return url
