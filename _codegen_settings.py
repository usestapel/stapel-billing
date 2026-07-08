"""Single-module Django settings for stapel-billing's harnesses.

Single source of truth for the ``settings.configure(...)`` block shared by:

  - the pytest suite (``conftest.py``) — mounts billing on its canonical
    ``billing/api/`` urlconf (``stapel_billing.tests.urls``), the historical
    test layout (billing's tests already mount at the canonical prefix,
    unlike auth's historical bare mount); and
  - the contract-emission harness (``_codegen.py`` / ``make contract``) —
    mounts billing on the same canonical public API prefix
    (``stapel_billing.codegen_urls`` → ``billing/api/``) and enables
    drf-spectacular, so the emitted ``schema.json`` / ``flows.json`` paths are
    byte-identical to the monolith aggregate's billing slice
    (contract-pipeline.md §2).

Keeping one copy here means the harness and the tests can never drift in their
``INSTALLED_APPS`` / ``MIDDLEWARE`` / mock config — the exact hazard
contract-pipeline.md §3 calls out ("~30 lines that *reference* the already-existing
config, not a second copy of it").
"""
from __future__ import annotations


def settings_kwargs(
    *,
    root_urlconf: str = "stapel_billing.tests.urls",
    contract: bool = False,
) -> dict:
    """Return the ``settings.configure(**kwargs)`` for a single-module billing instance.

    ``root_urlconf`` selects the mount: the test urlconf
    (``stapel_billing.tests.urls``) for the test suite, or the codegen urlconf
    (``stapel_billing.codegen_urls``) for contract emission — both already mount
    at the canonical ``billing/api/`` prefix, so (unlike auth) this is not the
    mount-prefix fix; it exists to keep the two harnesses on one settings source.

    ``contract=True`` swaps in the *production* ``REST_FRAMEWORK`` (the canonical
    stapel-core config, inlined as plain dotted paths — importing it would trip the
    same chicken-and-egg as spectacular). This matters for byte-identity: the
    monolith emits with ``DEFAULT_SCHEMA_CLASS=PermissionAwareAutoSchema`` and the
    real permission/renderer classes, and DRF caches ``REST_FRAMEWORK`` on first
    access, so it must be right at ``configure()`` time — a post-hoc assignment is
    too late. The test suite keeps its historical config (``contract=False``: no
    ``REST_FRAMEWORK`` override at all, matching pre-extraction behavior).

    ``SPECTACULAR_SETTINGS`` is deliberately *not* set. drf-spectacular builds its
    settings singleton at *import* time (``getattr(settings, 'SPECTACULAR_SETTINGS',
    {})`` at module load), before a ``configure()``-based harness can populate it,
    so a Django-level ``SPECTACULAR_SETTINGS`` is silently ignored and the emitter
    runs on drf **defaults**. That is exactly what the monolith aggregate emits with
    too — its ``SPECTACULAR_SETTINGS`` is ignored the same way (the committed
    ``schema.json`` has ``info.title=""``, no ``bearerAuth`` scheme, no
    ``x-stapel-*`` extensions). The one knob that still must be forced,
    ``SCHEMA_PATH_PREFIX``, is patched on the singleton directly by the harness
    (see ``_codegen._configure``).
    """
    installed_apps = [
        "django.contrib.contenttypes",
        "django.contrib.auth",
        "django.contrib.sessions",
        "django.contrib.messages",
        # contrib.admin so the ModelAdmin registrations in admin.py
        # are importable (and covered) in tests.
        "django.contrib.admin",
        # CommonDjangoConfig ships the stapel_core management commands
        # (generate_error_keys, used by the errors.json drift gate).
        "stapel_core.django.apps.CommonDjangoConfig",
        "stapel_core.django.users",
        "rest_framework",
        "drf_spectacular",
        "stapel_billing",
    ]

    if contract:
        # Mirror stapel_core.django.settings.REST_FRAMEWORK exactly (the config the
        # monolith emits under). Inlined, not imported, to dodge the import-time
        # settings read; kept in lockstep by test_contract.py's identity gate.
        rest_framework = {
            "DEFAULT_AUTHENTICATION_CLASSES": [
                "stapel_core.django.jwt.authentication.JWTCookieAuthentication",
            ],
            "DEFAULT_PERMISSION_CLASSES": [
                "stapel_core.django.api.permissions.IsServiceRequest",
                "stapel_core.django.api.permissions.IsSuperUser",
            ],
            "DEFAULT_RENDERER_CLASSES": [
                "rest_framework.renderers.JSONRenderer",
                "rest_framework.renderers.BrowsableAPIRenderer",
            ],
            "DEFAULT_SCHEMA_CLASS": "stapel_core.django.openapi.schemas.PermissionAwareAutoSchema",
            "EXCEPTION_HANDLER": "stapel_core.django.api.errors.stapel_exception_handler",
        }
    else:
        # No REST_FRAMEWORK override in tests — historical behavior, preserved
        # verbatim by this extraction (the pre-extraction conftest never set it).
        rest_framework = None

    kwargs = dict(
        SECRET_KEY="test-secret-key-not-for-production",
        INSTALLED_APPS=installed_apps,
        AUTH_USER_MODEL="users.User",
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        USE_TZ=True,
        ROOT_URLCONF=root_urlconf,
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            }
        },
        # In-memory bus — no Kafka/Redis broker needed
        STAPEL_BUS_BACKEND="stapel_core.bus.backends.memory.MemoryBus",
        # Synchronous in-process action delivery (no outbox table needed)
        # and schema validation ON so emits are checked against
        # schemas/emits/*.json in tests.
        STAPEL_COMM={
            "OUTBOX_ENABLED": False,
            "ACTION_TRANSPORT": "inprocess",
            "VALIDATE_SCHEMAS": True,
        },
        MIDDLEWARE=[
            "django.middleware.common.CommonMiddleware",
            "stapel_core.django.jwt.middleware.ServiceAPIKeyMiddleware",
        ],
        SERVICE_API_KEY="",
        # Checkout/portal redirect fallbacks derive from this when the
        # request and STAPEL_BILLING don't provide URLs.
        FRONTEND_URL="https://front.example",
        # Skip migrations — create tables directly from models
        MIGRATION_MODULES={
            "users": None,
            "billing": None,
        },
    )
    if rest_framework is not None:
        kwargs["REST_FRAMEWORK"] = rest_framework
    return kwargs


# The multi-module common path prefix drf-spectacular auto-detects in the monolith
# aggregate. Forced on the drf-spectacular settings singleton by the harness so a
# single-module instance derives the same operationIds (see _codegen._configure and
# the SCHEMA_PATH_PREFIX note above). Uniform across all five pair-backends.
CODEGEN_SCHEMA_PATH_PREFIX = "/"
