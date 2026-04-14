"""
Verification helpers for the NopeCHA captcha-solving service.

The browser-side flow loads NopeCHA as a Chrome extension and injects the API key
via https://nopecha.com/setup#KEY. That flow can fail silently — the extension may
keep using a stale key persisted from a previous run, or the setup page may not
finish writing to chrome.storage before we close it.

This module provides an independent, network-level check: it talks directly to the
NopeCHA HTTP API to confirm the key is real and accepted by their backend, without
involving the browser or extension at all.
"""

import httpx

from .config import HTTP_TIMEOUT_S

# NopeCHA status endpoint — referenced in the extension JS as `api.status: "/v1/status"`
# under base `https://api.nopecha.com`. Returns JSON with plan, credit, and key state.
NOPECHA_STATUS_URL = "https://api.nopecha.com/status"


class NopechaKeyError(RuntimeError):
    """Raised when the NopeCHA API key cannot be verified against the backend."""


def verify_api_key(api_key: str) -> None:
    """
    Verify that `api_key` is recognized by the NopeCHA backend.

    Raises NopechaKeyError on any failure (network error, non-2xx status,
    error field in the response body). On success, prints the plan and credit
    info if the response contains them.

    Note: response shape is parsed defensively. We do not require specific
    fields to be present — we only treat the key as invalid if the backend
    explicitly returns an error.
    """
    try:
        response = httpx.get(
            NOPECHA_STATUS_URL,
            params={"key": api_key},
            timeout=HTTP_TIMEOUT_S,
        )
    except httpx.HTTPError as e:
        raise NopechaKeyError(f"Failed to reach NopeCHA API: {e}") from e

    if response.status_code != 200:
        raise NopechaKeyError(
            f"NopeCHA API returned HTTP {response.status_code}: {response.text[:200]}"
        )

    try:
        data = response.json()
    except ValueError as e:
        raise NopechaKeyError(f"NopeCHA API returned non-JSON body: {response.text[:200]}") from e

    # The API uses an "error" field (numeric code or message) to signal invalid keys,
    # exhausted credits, IP bans, etc. Anything else we treat as success.
    if isinstance(data, dict) and "error" in data:
        raise NopechaKeyError(f"NopeCHA rejected the API key: {data['error']}")

    # Best-effort logging of plan/credit info — fields may vary by plan type.
    if isinstance(data, dict):
        plan = data.get("plan") or data.get("subscription")
        credit = data.get("credit") or data.get("credits") or data.get("balance")
        print(f"[NopeCHA] key verified (plan={plan}, credit={credit})")
    else:
        print("[NopeCHA] key verified")
