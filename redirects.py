"""Origin allowlist for payment-provider redirect targets.

Checkout and the customer portal hand the browser back to a URL this
service chose. A caller-supplied ``success_url`` / ``cancel_url`` /
``return_url`` therefore turns an authenticated billing endpoint into a
redirector: the provider itself renders the link, so the victim sees a
legitimate payment page before landing on the attacker's origin.

One allowlist, one resolver, every redirect parameter. The set of
acceptable origins is derived from configuration only — the request never
extends it:

* ``STAPEL_BILLING["REDIRECT_ALLOWED_ORIGINS"]`` — the explicit list;
* the origins of the configured fallbacks (``CHECKOUT_SUCCESS_URL``,
  ``CHECKOUT_CANCEL_URL``, ``PORTAL_RETURN_URL``);
* the origin of the flat ``FRONTEND_URL`` setting/env.

The last two mean a single-frontend deployment that already works keeps
working without declaring anything new.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

from django.conf import settings
from stapel_core.django.api.errors import StapelValidationError

from .conf import allow_unvalidated_redirect_urls, billing_settings
from .errors import (
    ERR_400_REDIRECT_URL_NOT_ALLOWED,
    ERR_400_REDIRECT_URL_NOT_CONFIGURED,
)

#: Schemes a browser redirect may use. ``javascript:``/``data:`` and
#: friends carry no origin and must never reach the provider.
_ALLOWED_SCHEMES = ("http", "https")

_DEFAULT_PORTS = {"http": "80", "https": "443"}


def origin_of(url: str) -> str | None:
    """Normalised ``scheme://host[:port]`` of *url*, or None if it has none.

    Returns None for anything that is not an absolute http(s) URL — a
    relative path, a scheme-relative ``//evil.example`` reference, or an
    exotic scheme. Callers treat None as "not allowlistable".
    """
    if not url or not isinstance(url, str):
        return None
    # A backslash is a path separator to browsers but not to urlsplit;
    # refusing it outright avoids the classic parser-differential bypass.
    if "\\" in url:
        return None
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in _ALLOWED_SCHEMES or not parts.netloc:
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    scheme = parts.scheme.lower()
    port = str(parts.port) if parts.port else _DEFAULT_PORTS[scheme]
    if port == _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def frontend_url() -> str:
    """The flat ``FRONTEND_URL`` setting, falling back to the environment."""
    return getattr(settings, "FRONTEND_URL", "") or os.environ.get(  # noqa: CFG001
        "FRONTEND_URL", ""
    )


def allowed_origins() -> set[str]:
    """Every origin a caller-supplied redirect target may point at."""
    candidates = list(billing_settings.REDIRECT_ALLOWED_ORIGINS or [])
    candidates.extend(
        getattr(billing_settings, key)
        for key in ("CHECKOUT_SUCCESS_URL", "CHECKOUT_CANCEL_URL", "PORTAL_RETURN_URL")
    )
    candidates.append(frontend_url())
    return {o for o in (origin_of(c) for c in candidates) if o}


def resolve(explicit: str | None, setting_key: str, path: str) -> str:
    """Resolve one redirect target — allowlisted, never a placeholder domain.

    Order: caller value (allowlist-checked) → ``STAPEL_BILLING[setting_key]``
    → derived from ``FRONTEND_URL``. Operator-configured values are trusted
    as configuration; only the request-supplied one is validated. With none
    of them available the request is rejected.
    """
    if explicit:
        _assert_allowed(explicit)
        return explicit
    configured = getattr(billing_settings, setting_key)
    if configured:
        return configured
    frontend = frontend_url()
    if frontend:
        return frontend.rstrip("/") + path
    raise StapelValidationError(ERR_400_REDIRECT_URL_NOT_CONFIGURED)


def _assert_allowed(url: str) -> None:
    if allow_unvalidated_redirect_urls():
        return
    origin = origin_of(url)
    if origin is None or origin not in allowed_origins():
        raise StapelValidationError(ERR_400_REDIRECT_URL_NOT_ALLOWED)
