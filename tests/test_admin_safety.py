"""Admin safety: the balance is not an editable field (0.11.0, audit major #8).

``Wallet.balance`` is a cache of the live lots, not the truth. Editing it in
the admin wrote a number the lots do not support — no lot moved, no ledger
row explained it — and the next operation, which recomputes the cache under
the wallet's row lock, silently threw the edit away. The honest way to
change a balance from the admin is to move real credits, which is what the
action does.
"""

import pytest
from django.contrib.admin.sites import AdminSite

from stapel_billing import services
from stapel_billing.admin import WalletAdmin
from stapel_billing.models import (
    CreditDebt,
    LotSource,
    Transaction,
    TransactionType,
    Wallet,
)


class _Request:
    """The two things the action reads off a request."""

    def __init__(self, post, user):
        self.POST = post
        self.user = user
        self.messages = []

    # django.contrib.messages needs a backend; the admin calls
    # message_user, which we intercept below instead.


@pytest.fixture
def wallet_admin():
    admin = WalletAdmin(Wallet, AdminSite())
    sent = []
    admin.message_user = lambda request, message, level=None: sent.append(
        (message, level)
    )
    admin._sent = sent
    return admin


@pytest.mark.django_db
class TestBalanceIsReadOnly:
    def test_the_cache_cannot_be_hand_edited(self, wallet_admin):
        assert "balance" in wallet_admin.readonly_fields

    def test_the_ledger_columns_that_explain_it_stay_readable(self, wallet_admin):
        # Still listed — an operator must be able to SEE the balance.
        assert "balance" in wallet_admin.list_display


@pytest.mark.django_db
class TestGrantCreditsAction:
    def test_grants_through_the_service_and_writes_a_ledger_row(
        self, wallet_admin, user, admin_user
    ):
        wallet = services.get_or_create_wallet(user)
        request = _Request({"grant_credits": "40", "grant_reason": "goodwill"}, admin_user)

        wallet_admin.grant_credits(request, Wallet.objects.filter(pk=wallet.pk))

        wallet.refresh_from_db()
        assert wallet.balance == 40
        txn = Transaction.objects.get()
        assert txn.type == TransactionType.ADJUSTMENT
        assert txn.credits_delta == 40
        assert txn.balance_after == 40
        assert txn.description == "Admin grant: goodwill"
        assert txn.metadata["admin_grant"] is True
        assert txn.metadata["reason"] == "goodwill"
        # A manual grant has no billing period behind it, so no deadline.
        assert txn.lot.expires_at is None
        assert txn.lot.source == LotSource.ADJUSTMENT

    def test_a_reason_is_required(self, wallet_admin, user, admin_user):
        wallet = services.get_or_create_wallet(user)
        request = _Request({"grant_credits": "40", "grant_reason": "  "}, admin_user)

        wallet_admin.grant_credits(request, Wallet.objects.filter(pk=wallet.pk))

        assert Transaction.objects.count() == 0
        assert "reason" in wallet_admin._sent[0][0]

    def test_a_non_positive_amount_is_refused(self, wallet_admin, user, admin_user):
        wallet = services.get_or_create_wallet(user)
        request = _Request({"grant_credits": "0", "grant_reason": "oops"}, admin_user)

        wallet_admin.grant_credits(request, Wallet.objects.filter(pk=wallet.pk))
        assert Transaction.objects.count() == 0

        request = _Request({"grant_credits": "", "grant_reason": "oops"}, admin_user)
        wallet_admin.grant_credits(request, Wallet.objects.filter(pk=wallet.pk))
        assert Transaction.objects.count() == 0

    def test_it_settles_an_outstanding_debt_like_any_other_credit(
        self, wallet_admin, user, admin_user
    ):
        services.debit(
            user=user, credits=10, type=TransactionType.AI_CHARGE, allow_partial=True
        )
        wallet = services.get_or_create_wallet(user)
        request = _Request({"grant_credits": "25", "grant_reason": "make good"}, admin_user)

        wallet_admin.grant_credits(request, Wallet.objects.filter(pk=wallet.pk))

        wallet.refresh_from_db()
        assert wallet.balance == 15
        assert CreditDebt.objects.get().settled_at is not None

    def test_an_erased_owners_wallet_is_skipped_rather_than_crashing(
        self, wallet_admin, user, admin_user
    ):
        wallet = services.get_or_create_wallet(user)
        Wallet.objects.filter(pk=wallet.pk).update(user=None)
        request = _Request({"grant_credits": "5", "grant_reason": "x"}, admin_user)

        wallet_admin.grant_credits(request, Wallet.objects.filter(pk=wallet.pk))

        assert Transaction.objects.count() == 0
        assert "no owner" in wallet_admin._sent[0][0]


@pytest.fixture
def admin_user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_superuser(
        username="root", email="root@example.com", password="rootpass123"
    )
