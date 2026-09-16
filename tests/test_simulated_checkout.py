"""Staff buy credits without a card — through the REAL post-payment path.

Staff have to exercise the product the way a customer meets it, and "buy
credits" is the one step they cannot take: it ends at a card. The alternative
people reach for is a mock payment provider behind an environment flag, which
this fleet forbids — a deployment must not behave differently because of a
setting.

So the gate is WHO, never a setting, and the simulation is of the RETURN from
the processor, not of the grant. Everything after that return is the same code
a real purchase runs: ownership, catalogue reconciliation, the
one-grant-per-session claim, `credit()`, the ledger row, the lot, and
`payment.completed` with its notification. A shortcut that credited a wallet
would exercise none of it.

And it is MARKED. The account belongs to a real person, so nothing about the
id can distinguish a simulated purchase from a paid one — only the flag can.
"""
import pytest
from django.contrib.auth import get_user_model

from stapel_billing import services
from stapel_billing.catalog import CREDIT_PACKAGES_BY_SLUG
from stapel_billing.models import Transaction, TransactionType


@pytest.fixture
def staff_only(db):
    return get_user_model().objects.create_user(
        username="qa", email="qa@example.com", password="qapass123456", is_staff=True
    )


@pytest.fixture
def package():
    return next(iter(CREDIT_PACKAGES_BY_SLUG.values()))


@pytest.mark.django_db
class TestItRunsTheRealPath:
    def test_an_empty_wallet_ends_with_the_package_credits(self, staff_only, package):
        assert services.get_or_create_wallet(staff_only).balance == 0

        result = services.simulate_checkout_completed(
            user=staff_only, package=package.slug, actor="qa"
        )

        assert result["credits"] == package.credits
        assert result["balance"] == package.credits

    def test_it_writes_the_ledger_row_a_purchase_writes(self, staff_only, package):
        services.simulate_checkout_completed(
            user=staff_only, package=package.slug, actor="qa"
        )
        txn = Transaction.objects.get()
        # Same type and same money as a real purchase — this is the point:
        # the reporting shape is exercised, not sidestepped.
        assert txn.type == TransactionType.CREDIT_PURCHASE
        assert txn.amount_cents == package.price_cents
        assert txn.credits_delta == package.credits
        # ...and a non-expiring lot, because a purchase is cash.
        assert txn.lot.expires_at is None

    def test_the_session_is_claimed_so_it_cannot_double_grant(
        self, staff_only, package
    ):
        result = services.simulate_checkout_completed(
            user=staff_only, package=package.slug, actor="qa"
        )
        # Replaying the same session id through the real handler grants once.
        services.handle_checkout_completed(
            {
                "data": {
                    "object": {
                        "id": result["session_id"],
                        "mode": "payment",
                        "payment_status": "paid",
                        "status": "complete",
                        "currency": package.currency.lower(),
                        "amount_total": package.price_cents,
                        "client_reference_id": str(staff_only.id),
                        "metadata": {
                            "user_id": str(staff_only.id),
                            "package": package.slug,
                            "simulated": "true",
                        },
                    }
                }
            }
        )
        assert Transaction.objects.count() == 1

    def test_an_unknown_package_is_refused(self, staff_only):
        with pytest.raises(ValueError):
            services.simulate_checkout_completed(
                user=staff_only, package="no-such-package", actor="qa"
            )
        assert Transaction.objects.count() == 0


@pytest.mark.django_db
class TestItIsDistinguishableFromRealMoney:
    def test_the_ledger_row_carries_the_flag(self, staff_only, package):
        services.simulate_checkout_completed(
            user=staff_only, package=package.slug, actor="qa"
        )
        assert Transaction.objects.get().metadata["simulated"] is True

    def test_a_revenue_query_can_exclude_it(self, staff_only, package):
        """The column is `Transaction.metadata`, and `real_money()` is the query."""
        services.simulate_checkout_completed(
            user=staff_only, package=package.slug, actor="qa"
        )
        purchases = Transaction.objects.filter(type=TransactionType.CREDIT_PURCHASE)
        assert purchases.count() == 1
        assert services.real_money(purchases).count() == 0

    def test_the_naive_exclude_is_the_trap_real_money_exists_to_avoid(
        self, staff_only, package
    ):
        """`exclude(metadata__simulated=True)` ALSO drops rows with no key.

        Pinned because it is the query a reasonable person writes, it looks
        right, and the only symptom in production is a revenue number quietly
        too low. Rows written before 0.18.0 carry no key at all.
        """
        Transaction.objects.create(
            wallet=services.get_or_create_wallet(staff_only),
            type=TransactionType.CREDIT_PURCHASE,
            credits_delta=100,
            balance_after=100,
            metadata={},  # a pre-0.18.0 purchase: real money, no flag
        )
        purchases = Transaction.objects.filter(type=TransactionType.CREDIT_PURCHASE)
        # The trap: the real one disappears too.
        assert purchases.exclude(metadata__simulated=True).count() == 0
        # The helper keeps it.
        assert services.real_money(purchases).count() == 1

    def test_a_real_purchase_is_not_marked(self, staff_only, package):
        """So the exclude above cannot pass by marking everything."""
        services.handle_checkout_completed(
            {
                "data": {
                    "object": {
                        "id": "cs_real_1",
                        "mode": "payment",
                        "payment_status": "paid",
                        "status": "complete",
                        "currency": package.currency.lower(),
                        "amount_total": package.price_cents,
                        "client_reference_id": str(staff_only.id),
                        "metadata": {
                            "user_id": str(staff_only.id),
                            "package": package.slug,
                        },
                    }
                }
            }
        )
        txn = Transaction.objects.get()
        # Written, and written FALSE — so "real money" is a positive filter.
        assert txn.metadata["simulated"] is False
        purchases = Transaction.objects.filter(type=TransactionType.CREDIT_PURCHASE)
        assert services.real_money(purchases).count() == 1


@pytest.mark.django_db
class TestTheEndpointGateIsTheServer:
    def _post(self, client, package_slug):
        return client.post(
            "/billing/api/v1/checkout/simulate",
            {"package": package_slug},
            format="json",
        )

    def test_a_staff_account_gets_credits(self, staff_only, package):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=staff_only)
        response = self._post(client, package.slug)
        assert response.status_code == 200, response.data
        assert response.data["credits"] == package.credits
        assert response.data["simulated"] is True

    def test_a_NON_staff_account_is_refused_by_the_server(self, db, package):
        """A hidden checkbox is not a gate — so the body is sent anyway."""
        from rest_framework.test import APIClient

        customer = get_user_model().objects.create_user(
            username="customer", email="c@example.com", password="custpass12345"
        )
        assert customer.is_staff is False
        client = APIClient()
        client.force_authenticate(user=customer)

        response = self._post(client, package.slug)

        assert response.status_code == 403
        assert Transaction.objects.count() == 0
        assert services.get_or_create_wallet(customer).balance == 0

    def test_an_anonymous_request_is_refused(self, db, package):
        from rest_framework.test import APIClient

        response = self._post(APIClient(), package.slug)
        assert response.status_code in (401, 403)
        assert Transaction.objects.count() == 0

    def test_the_client_cannot_choose_the_amount(self, staff_only, package):
        """Only the package travels; credits and price come from the catalogue."""
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=staff_only)
        response = client.post(
            "/billing/api/v1/checkout/simulate",
            {"package": package.slug, "credits": 1_000_000, "price_cents": 1},
            format="json",
        )
        assert response.status_code == 200
        assert response.data["credits"] == package.credits
