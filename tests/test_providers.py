"""Provider abstraction, lazy configuration, catalog overrides, events."""

import json
from pathlib import Path

import pytest

from stapel_billing import catalog, services
from stapel_billing.conf import billing_settings
from stapel_billing.models import LotSource, Subscription, Transaction, TransactionType
from stapel_billing.providers.base import PaymentProvider
from stapel_billing.providers.stripe import StripeProvider


@pytest.fixture(autouse=True)
def _reset_billing_settings():
    yield
    billing_settings.reload()


class DummyProvider(PaymentProvider):
    """Test double registered by dotted path."""

    name = "dummy"

    def create_checkout_session(self, *, user, package, plan, success_url, cancel_url):
        return ("https://dummy.example/checkout", "dummy_session_1")

    def create_portal_session(self, *, customer_id, return_url):
        return "https://dummy.example/portal"

    def cancel_subscription(self, subscription_id):
        return None

    def verify_webhook(self, payload, signature):
        return json.loads(payload)


DUMMY_PATH = f"{DummyProvider.__module__}.{DummyProvider.__qualname__}"


@pytest.mark.django_db
class TestProviderSelection:
    def test_default_provider_is_stripe(self, settings):
        provider = services.get_provider()
        assert isinstance(provider, StripeProvider)
        assert provider.name == "stripe"

    def test_provider_swap_via_dotted_path(self, settings):
        settings.STAPEL_BILLING = {"PAYMENT_PROVIDER": DUMMY_PATH}
        provider = services.get_provider()
        assert isinstance(provider, DummyProvider)

    def test_checkout_uses_swapped_provider(self, authed_client, settings):
        settings.STAPEL_BILLING = {"PAYMENT_PROVIDER": DUMMY_PATH}
        resp = authed_client.post(
            "/billing/api/checkout",
            {
                "package": "starter",
                "success_url": "https://front.example/ok",
                "cancel_url": "https://front.example/no",
            },
            format="json",
        )
        assert resp.status_code == 200
        assert resp.json()["checkout_url"] == "https://dummy.example/checkout"
        assert resp.json()["session_id"] == "dummy_session_1"

    def test_non_provider_class_rejected(self, settings):
        settings.STAPEL_BILLING = {"PAYMENT_PROVIDER": "stapel_billing.models.Wallet"}
        with pytest.raises(TypeError):
            services.get_provider()


class TestLazyStripeKeys:
    def test_keys_read_lazily_from_env(self, monkeypatch):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_env_after_import")
        billing_settings.reload()
        assert StripeProvider().api_key == "sk_env_after_import"

    def test_settings_override_beats_env(self, settings, monkeypatch):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_env_should_lose")
        settings.STRIPE_SECRET_KEY = ""  # triggers billing_settings reload
        assert StripeProvider().api_key == ""

    def test_stripe_disabled_fixture_takes_effect(
        self, stripe_disabled, monkeypatch
    ):
        # Even if a key was present at import time, the fixture wins now
        # because reads happen at call time through the conf layer.
        assert StripeProvider().api_key == ""
        with pytest.raises(ValueError):
            services.verify_stripe_signature(b"{}", "sig")


@pytest.mark.django_db
class TestCatalogOverride:
    def test_catalog_override_via_settings(self, settings, api_client):
        settings.STAPEL_BILLING = {
            "CREDIT_PACKAGES": [
                {"slug": "mini", "name": "Mini", "credits": 100, "price_cents": 200},
            ],
        }
        assert set(catalog.CREDIT_PACKAGES_BY_SLUG) == {"mini"}
        pkg = catalog.CREDIT_PACKAGES_BY_SLUG["mini"]
        assert isinstance(pkg, catalog.CreditPackage)
        assert pkg.credits == 100
        # Plans keep their defaults (per-key resolution).
        assert "pro" in catalog.PLANS_BY_SLUG

        resp = api_client.get("/billing/api/products")
        assert resp.status_code == 200
        assert {p["slug"] for p in resp.json()["packages"]} == {"mini"}

    def test_defaults_without_override(self):
        assert set(catalog.CREDIT_PACKAGES_BY_SLUG) >= {"starter", "standard", "bulk"}
        assert catalog.PLANS_BY_SLUG["pro"].monthly_credits_included == 300

    def test_checkout_accepts_overridden_package(self, authed_client, settings):
        settings.STAPEL_BILLING = {
            "PAYMENT_PROVIDER": DUMMY_PATH,
            "CREDIT_PACKAGES": [
                {"slug": "mini", "name": "Mini", "credits": 100, "price_cents": 200},
            ],
        }
        resp = authed_client.post(
            "/billing/api/checkout",
            {
                "package": "mini",
                "success_url": "https://front.example/ok",
                "cancel_url": "https://front.example/no",
            },
            format="json",
        )
        assert resp.status_code == 200
        # The old default slugs are gone from validation too.
        resp = authed_client.post(
            "/billing/api/checkout",
            {
                "package": "starter",
                "success_url": "https://front.example/ok",
                "cancel_url": "https://front.example/no",
            },
            format="json",
        )
        assert resp.status_code == 400


@pytest.mark.django_db
class TestIdempotentDebit:
    def _debit(self, api_client, user, key):
        return api_client.post(
            "/billing/api/internal/debit",
            {
                "user_id": str(user.id),
                "credits": 100,
                "type": "ai_charge",
                "idempotency_key": key,
            },
            format="json",
            HTTP_X_API_KEY="test-service-key",
        )

    def test_duplicate_debit_short_circuits(self, api_client, user, settings):
        settings.SERVICE_API_KEY = "test-service-key"
        services.credit(
            user=user,
            credits=500,
            type=TransactionType.CREDIT_PURCHASE,
            source=LotSource.PURCHASE,
        )

        first = self._debit(api_client, user, "job-42")
        assert first.status_code == 200, first.content
        assert first.json()["balance_after"] == 400

        second = self._debit(api_client, user, "job-42")
        assert second.status_code == 200
        assert second.json() == first.json()

        wallet = user.wallet
        wallet.refresh_from_db()
        assert wallet.balance == 400
        debits = Transaction.objects.filter(wallet=wallet, credits_delta=-100)
        assert debits.count() == 1
        assert debits.first().metadata["idempotency_key"] == "job-42"

    def test_different_keys_debit_separately(self, api_client, user, settings):
        settings.SERVICE_API_KEY = "test-service-key"
        services.credit(
            user=user,
            credits=500,
            type=TransactionType.CREDIT_PURCHASE,
            source=LotSource.PURCHASE,
        )
        assert self._debit(api_client, user, "a").json()["balance_after"] == 400
        assert self._debit(api_client, user, "b").json()["balance_after"] == 300

    def test_service_debit_without_key_still_works(self, api_client, user, settings):
        settings.SERVICE_API_KEY = "test-service-key"
        services.credit(
            user=user,
            credits=500,
            type=TransactionType.CREDIT_PURCHASE,
            source=LotSource.PURCHASE,
        )
        resp = api_client.post(
            "/billing/api/internal/debit",
            {"user_id": str(user.id), "credits": 100, "type": "ai_charge"},
            format="json",
            HTTP_X_API_KEY="test-service-key",
        )
        assert resp.status_code == 200


SCHEMAS_DIR = Path(catalog.__file__).resolve().parent / "schemas" / "emits"


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMAS_DIR / f"{name}.json").read_text())


@pytest.mark.django_db(transaction=False)
class TestWebhookMilestones:
    def _checkout_event(self, user, **metadata):
        """A settled session, shaped the way Stripe actually sends one.

        The handler reconciles mode/payment_status/currency/amount against
        the catalog entry the metadata names, so a session that omitted
        those fields would only ever exercise the refusal path.
        """
        if metadata.get("package"):
            entry = catalog.CREDIT_PACKAGES_BY_SLUG[metadata["package"]]
            mode = "payment"
        else:
            entry = catalog.PLANS_BY_SLUG[metadata["plan"]]
            mode = "subscription"
        return {
            "id": "evt_1",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_1",
                    "customer": "cus_1",
                    "subscription": "sub_1",
                    "mode": mode,
                    "payment_status": "paid",
                    "currency": entry.currency.lower(),
                    "amount_total": entry.price_cents,
                    "client_reference_id": str(user.id),
                    "metadata": {"user_id": str(user.id), **metadata},
                }
            },
        }

    def test_package_checkout_emits_payment_completed(self, user):
        import jsonschema

        from stapel_core.comm import action_registry
        from stapel_core.signals import payment_completed

        received_actions = []
        action_registry.subscribe(
            "payment.completed", lambda event: received_actions.append(event)
        )
        received_signals = []
        payment_completed.connect(
            lambda sender, **kw: received_signals.append(kw), weak=False
        )

        services.handle_checkout_completed(
            self._checkout_event(user, package="starter")
        )

        assert len(received_actions) == 1
        payload = received_actions[0].payload
        jsonschema.validate(payload, _load_schema("payment.completed"))
        assert payload["user_id"] == str(user.id)
        assert payload["amount_cents"] == 500
        assert payload["currency"] == "usd"
        txn = Transaction.objects.get(id=payload["transaction_id"])
        assert txn.credits_delta == 500

        assert len(received_signals) == 1
        assert received_signals[0]["user"] == user
        assert received_signals[0]["transaction"].id == txn.id

    def test_plan_checkout_emits_subscription_changed(self, user):
        import jsonschema

        from stapel_core.comm import action_registry
        from stapel_core.signals import subscription_changed

        received_actions = []
        action_registry.subscribe(
            "subscription.changed", lambda event: received_actions.append(event)
        )
        received_signals = []
        subscription_changed.connect(
            lambda sender, **kw: received_signals.append(kw), weak=False
        )

        services.handle_checkout_completed(self._checkout_event(user, plan="pro"))

        assert len(received_actions) == 1
        payload = received_actions[0].payload
        jsonschema.validate(payload, _load_schema("subscription.changed"))
        assert payload == {
            "user_id": str(user.id),
            "plan": "pro",
            "status": "active",
            "current_period_end": None,
        }
        assert len(received_signals) == 1
        assert isinstance(received_signals[0]["subscription"], Subscription)

    def test_subscription_deleted_emits_subscription_changed(self, user):
        from stapel_core.comm import action_registry

        Subscription.objects.create(
            user=user, plan="pro", status="active", stripe_subscription_id="sub_9"
        )
        received = []
        action_registry.subscribe(
            "subscription.changed", lambda event: received.append(event)
        )
        services.handle_subscription_deleted(
            {"data": {"object": {"id": "sub_9"}}}
        )
        assert len(received) == 1
        assert received[0].payload["status"] == "cancelled"

    def test_subscription_updated_emits_real_period_end(self, user):
        """G11: current_period_end must carry the provider's real timestamp,
        not a null/zero placeholder, once a subscription object supplies it."""
        import datetime as _dt

        import jsonschema

        from stapel_core.comm import action_registry

        Subscription.objects.create(
            user=user, plan="pro", status="active", stripe_subscription_id="sub_up"
        )
        received = []
        action_registry.subscribe(
            "subscription.changed", lambda event: received.append(event)
        )
        # 2026-08-01T00:00:00Z / 2026-09-01T00:00:00Z as unix seconds.
        start = int(_dt.datetime(2026, 8, 1, tzinfo=_dt.timezone.utc).timestamp())
        end = int(_dt.datetime(2026, 9, 1, tzinfo=_dt.timezone.utc).timestamp())
        services.handle_subscription_updated(
            {
                "data": {
                    "object": {
                        "id": "sub_up",
                        "status": "active",
                        "current_period_start": start,
                        "current_period_end": end,
                    }
                }
            }
        )
        assert len(received) == 1
        payload = received[0].payload
        jsonschema.validate(payload, _load_schema("subscription.changed"))
        assert payload["current_period_end"] == "2026-09-01T00:00:00+00:00"
        # Model persisted both boundaries from real data.
        sub = Subscription.objects.get(stripe_subscription_id="sub_up")
        assert sub.current_period_start == _dt.datetime(
            2026, 8, 1, tzinfo=_dt.timezone.utc
        )
        assert sub.current_period_end == _dt.datetime(
            2026, 9, 1, tzinfo=_dt.timezone.utc
        )

    def test_subscription_updated_without_period_keeps_none(self, user):
        """A subscription object with no period leaves the field honestly
        null rather than fabricating a zero timestamp."""
        from stapel_core.comm import action_registry

        Subscription.objects.create(
            user=user, plan="pro", status="active", stripe_subscription_id="sub_np"
        )
        received = []
        action_registry.subscribe(
            "subscription.changed", lambda event: received.append(event)
        )
        services.handle_subscription_updated(
            {"data": {"object": {"id": "sub_np", "status": "past_due"}}}
        )
        assert len(received) == 1
        assert received[0].payload["current_period_end"] is None
        assert received[0].payload["status"] == "past_due"
        sub = Subscription.objects.get(stripe_subscription_id="sub_np")
        assert sub.current_period_end is None

    def test_subscription_updated_preserves_prior_period_when_absent(self, user):
        """A later event without period data must not null out a period that
        an earlier event already populated."""
        import datetime as _dt

        end = _dt.datetime(2026, 10, 1, tzinfo=_dt.timezone.utc)
        Subscription.objects.create(
            user=user,
            plan="pro",
            status="active",
            stripe_subscription_id="sub_keep",
            current_period_end=end,
        )
        services.handle_subscription_updated(
            {"data": {"object": {"id": "sub_keep", "status": "active"}}}
        )
        sub = Subscription.objects.get(stripe_subscription_id="sub_keep")
        assert sub.current_period_end == end

    def test_subscription_deleted_emits_real_period_end(self, user):
        """current_period_end from the deleted subscription object reaches the
        wire so consumers know when access actually lapses."""
        import datetime as _dt

        from stapel_core.comm import action_registry

        Subscription.objects.create(
            user=user, plan="pro", status="active", stripe_subscription_id="sub_del"
        )
        received = []
        action_registry.subscribe(
            "subscription.changed", lambda event: received.append(event)
        )
        end = int(_dt.datetime(2026, 12, 1, tzinfo=_dt.timezone.utc).timestamp())
        services.handle_subscription_deleted(
            {"data": {"object": {"id": "sub_del", "current_period_end": end}}}
        )
        assert len(received) == 1
        assert received[0].payload["status"] == "cancelled"
        assert received[0].payload["current_period_end"] == "2026-12-01T00:00:00+00:00"

    def test_invoice_paid_emits_payment_completed(self, user):
        import jsonschema

        from stapel_core.comm import action_registry

        Subscription.objects.create(
            user=user, plan="pro", status="active", stripe_subscription_id="sub_7"
        )
        received = []
        action_registry.subscribe(
            "payment.completed", lambda event: received.append(event)
        )
        event = {
            "data": {
                "object": {
                    "id": "in_1",
                    "subscription": "sub_7",
                    "billing_reason": "subscription_cycle",
                    "amount_paid": 1500,
                    "currency": "usd",
                }
            }
        }
        services.handle_invoice_paid(event)
        assert len(received) == 1
        payload = received[0].payload
        jsonschema.validate(payload, _load_schema("payment.completed"))
        assert payload["amount_cents"] == 1500
        # At-least-once redelivery must not double-grant or re-emit.
        services.handle_invoice_paid(event)
        assert len(received) == 1
