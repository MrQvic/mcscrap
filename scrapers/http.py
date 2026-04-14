import httpx

from .config import HTTP_TIMEOUT_S

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

AJAX_HEADERS = {
    **HEADERS,
    "X-Requested-With": "XMLHttpRequest",
}


def http_get(url: str, ajax: bool = False) -> str:
    """Fetch a URL and return response text. Raises on non-2xx."""
    headers = AJAX_HEADERS if ajax else HEADERS
    response = httpx.get(url, headers=headers, follow_redirects=True, timeout=HTTP_TIMEOUT_S)
    response.raise_for_status()
    return response.text
