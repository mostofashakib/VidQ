import ipaddress
import logging
from urllib.parse import urlparse

from app.logging_utils import log_suppressed

logger = logging.getLogger(__name__)


def is_safe_url(url: str) -> bool:
    """
    Return True only for http/https URLs that point to external hosts.

    Blocks all RFC 1918 private ranges, loopback, link-local (169.254/10),
    and reserved addresses to prevent SSRF.  The only carve-out is
    localhost/127.0.0.1 paths under /temp_storage/, which the frontend uses
    to reference locally-recorded blob files.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False

        hostname = parsed.hostname
        if not hostname:
            return False

        hostname_lower = hostname.lower()

        # Explicit allow-list: local temp_storage paths used by blob recordings
        if hostname_lower in ("localhost", "127.0.0.1", "0.0.0.0"):
            return parsed.path.startswith("/temp_storage/")

        # Block numeric IP addresses that resolve to internal ranges
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        except ValueError:
            pass  # not a numeric IP — treat as an external hostname

        return True
    except Exception as exc:
        log_suppressed(logger, f"URL safety check failed for {url!r}", exc, level="warning")
        return False


def filename_from_url(url: str, fallback_ext: str = "mp4") -> str:
    """Extract the bare filename from a URL path using proper URL parsing."""
    path = urlparse(url).path.rstrip("/")
    name = path.split("/")[-1] if path else ""
    if not name:
        return f"video.{fallback_ext}"
    # Ensure the extension is sane (alphanumeric, max 5 chars)
    if "." in name:
        base, ext = name.rsplit(".", 1)
        if ext.isalnum() and len(ext) <= 5:
            return name
        return f"{base}.{fallback_ext}"
    return f"{name}.{fallback_ext}"
