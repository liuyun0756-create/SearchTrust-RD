"""Explicit V10 acquisition scope; historical callers opt out."""
from urllib.parse import parse_qsl, unquote, urlsplit


def is_admin_page(url):
    if not url:
        return False
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return False  # Invalid URLs remain the existing URL validator's responsibility.
    segments = unquote(parts.path).casefold().split("/")
    return (any(s in {"wp-login.php", "wp-admin"} for s in segments)
            or any(key.casefold() == "action" for key, _ in parse_qsl(parts.query, keep_blank_values=True)))
