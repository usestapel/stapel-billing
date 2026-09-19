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

        # The catalogue the suite runs on is HOST-shaped: the shipped plans
        # plus one slug the Plan enum does not have (see tests/plans.py).
        # It is set here and not in _codegen_settings.py on purpose — the
        # contract harness shares that module, and what this library emits
        # must keep describing the library, not a test host's ladder.
        from stapel_billing.tests.plans import host_style_plans

        settings.STAPEL_BILLING = {
            **getattr(settings, "STAPEL_BILLING", {}),
            "PLANS": host_style_plans(),
        }


import pytest  # noqa: E402


# ─── emit() outside transaction.atomic() is an error here ───
#
# stapel-core owns the rule and the switch for it
# (``STAPEL_COMM["EMIT_OUTSIDE_ATOMIC"] = "error"``, stapel_core/comm/
# actions.py), and ``_codegen_settings.py`` asks for the strict mode — but
# that guard only runs when the outbox is on, and this suite runs with
# ``OUTBOX_ENABLED: False`` so that emits are delivered synchronously and
# validated against the committed schemas. Worse, pytest-django wraps every
# ``django_db`` test in ``transaction.atomic()``, so ``in_atomic_block`` is
# True inside a test whatever the code under test does: the production
# guard is structurally blind here, and a setting alone would be a gate that
# proves nothing.
#
# So the depth is measured instead. The baseline is taken in
# ``pytest_runtest_call`` — after every fixture, including the harness's own
# atomic wrapper, has been entered — and an ``emit()`` from library code is
# only accepted from strictly deeper than that, i.e. from a block the code
# under test opened itself. Same exception class as the production switch,
# so there is one vocabulary for the defect.
#
# tests/test_emit_atomicity.py asserts this guard is live, so it cannot rot
# back into inertness unnoticed.

_EMIT_OUTSIDE_ATOMIC_MESSAGE = (
    "emit(%r) called outside a transaction.atomic() block opened by the code "
    "under test: the outbox row commits detached from the mutation it "
    "describes. Wrap mutation+emit in stapel_core.comm.mutate_and_emit() (or "
    "transaction.atomic())."
)


def _atomic_depth() -> int:
    from django.db import transaction

    return len(transaction.get_connection().atomic_blocks)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    from stapel_core.comm.exceptions import EmitOutsideAtomicError

    from stapel_billing import services

    baseline = _atomic_depth()
    real_emit = services.emit

    def guarded_emit(name, payload=None, **kwargs):
        if _atomic_depth() <= baseline:
            raise EmitOutsideAtomicError(_EMIT_OUTSIDE_ATOMIC_MESSAGE % name)
        return real_emit(name, payload, **kwargs)

    services.emit = guarded_emit
    try:
        yield
    finally:
        services.emit = real_emit


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


@pytest.fixture
def stripe_placeholders_allowed(stripe_disabled, settings):
    """Unconfigured Stripe *plus* the explicit opt-in to dev placeholders.

    Since BILL-06 an unconfigured provider refuses instead of fabricating a
    checkout session, a portal link or a cancel nobody performed. The tests
    that are about the placeholder path itself therefore have to ask for it
    the way a developer's machine does — with
    ``ALLOW_UNCONFIGURED_PAYMENT_PROVIDER``.
    """
    settings.STAPEL_BILLING = {
        **getattr(settings, "STAPEL_BILLING", {}),
        "ALLOW_UNCONFIGURED_PAYMENT_PROVIDER": True,
    }
    return settings
