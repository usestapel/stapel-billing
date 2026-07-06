def pytest_configure(config):
    from django.conf import settings
    if not settings.configured:
        settings.configure(
            SECRET_KEY="test-secret-key-not-for-production",
            INSTALLED_APPS=[
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
                "stapel_billing",
            ],
            AUTH_USER_MODEL="users.User",
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": ":memory:",
                }
            },
            DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
            USE_TZ=True,
            ROOT_URLCONF="stapel_billing.tests.urls",
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
        import django
        django.setup()

        # Register schemas/emits/*.json with the comm registries so emit()
        # payloads are validated against the committed contracts in tests.
        from stapel_core.comm.schemas import autoload_schemas
        autoload_schemas()


import pytest  # noqa: E402


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model
    User = get_user_model()
    return User.objects.create_user(
        username="testuser",
        email="testuser@example.com",
        password="testpass123",
    )


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient
    return APIClient()


@pytest.fixture
def authed_client(user):
    from rest_framework.test import APIClient
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def stripe_disabled(settings, monkeypatch):
    """Disable Stripe regardless of the host environment.

    Keys are read lazily through stapel_billing.conf.billing_settings, so
    overriding the Django settings here actually takes effect at call
    time (they are no longer frozen at import via os.getenv).  The env
    vars are cleared too so the AppSettings env fallback cannot leak a
    real key into the test.
    """
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    settings.STRIPE_SECRET_KEY = ""
    settings.STRIPE_WEBHOOK_SECRET = ""
    return settings
