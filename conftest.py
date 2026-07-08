def pytest_configure(config):
    from django.conf import settings
    if not settings.configured:
        # Single source of truth for this block lives in _codegen_settings.py so
        # the test harness and the contract-emission harness (make contract) can
        # never drift (contract-pipeline.md §3). Tests keep the historical mount
        # + REST_FRAMEWORK config, exactly as before the extraction.
        from stapel_billing._codegen_settings import settings_kwargs

        settings.configure(**settings_kwargs())
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
