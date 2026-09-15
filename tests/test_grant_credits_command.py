"""`manage.py billing_grant_credits` — a grant path that works from a terminal.

Until 0.15.0 the only way to put credits on an account without taking a
payment was an admin action behind a browser session, so an operator on the
box, a deploy script or a support engineer over ssh had no way at all.

What the tests below pin down is mostly what the command REFUSES: a grant
that lands on nobody, or on the wrong one of two rows, is worse than no
grant, because the operator walks away believing it worked.
"""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from stapel_billing import services
from stapel_billing.models import LotSource, Transaction, TransactionType


def _run(**kwargs):
    from io import StringIO

    out = StringIO()
    call_command("billing_grant_credits", stdout=out, stderr=StringIO(), **kwargs)
    return out.getvalue()


@pytest.mark.django_db
class TestItGrants:
    def test_by_account_id(self, user):
        out = _run(
            account=str(user.pk),
            credits=120,
            reason="staff testing",
            actor="ops@example.com",
        )
        wallet = services.get_or_create_wallet(user)
        wallet.refresh_from_db()
        assert wallet.balance == 120
        txn = Transaction.objects.get()
        assert txn.type == TransactionType.ADJUSTMENT
        assert txn.credits_delta == 120
        assert txn.lot.source == LotSource.ADJUSTMENT
        assert txn.lot.expires_at is None
        assert txn.metadata["reason"] == "staff testing"
        assert txn.metadata["actor"] == "ops@example.com"
        assert txn.metadata["manual_grant"] is True
        assert "balance     0 -> 120" in out

    def test_by_email_case_insensitively(self, user):
        _run(
            account="TestUser@Example.com",
            credits=5,
            reason="r",
            actor="a",
        )
        wallet = services.get_or_create_wallet(user)
        wallet.refresh_from_db()
        assert wallet.balance == 5

    def test_the_output_names_the_account_by_short_id_only(self, user):
        out = _run(account=str(user.pk), credits=5, reason="r", actor="a")
        # This transcript ends up in deploy logs and chat.
        assert user.email not in out
        assert str(user.pk)[:8] in out

    def test_dry_run_writes_nothing(self, user):
        out = _run(
            account=str(user.pk),
            credits=50,
            reason="r",
            actor="a",
            dry_run=True,
        )
        assert Transaction.objects.count() == 0
        assert "dry run" in out
        assert "0 -> 50" in out


@pytest.mark.django_db
class TestItIsIdempotentUnderAnExplicitKey:
    def test_the_same_key_grants_once(self, user):
        _run(
            account=str(user.pk),
            credits=100,
            reason="r",
            actor="a",
            idempotency_key="grant-2026-09-16-01",
        )
        out = _run(
            account=str(user.pk),
            credits=100,
            reason="r",
            actor="a",
            idempotency_key="grant-2026-09-16-01",
        )
        wallet = services.get_or_create_wallet(user)
        wallet.refresh_from_db()
        assert wallet.balance == 100
        assert Transaction.objects.count() == 1
        # And it SAYS so: "granted" when nothing moved is the lie that makes
        # an idempotent command untrustworthy.
        assert "already granted" in out

    def test_without_a_key_two_runs_are_two_grants(self, user):
        for _ in range(2):
            _run(account=str(user.pk), credits=100, reason="r", actor="a")
        wallet = services.get_or_create_wallet(user)
        wallet.refresh_from_db()
        # "Give them another 100" is a real instruction.
        assert wallet.balance == 200
        assert Transaction.objects.count() == 2


@pytest.mark.django_db
class TestItRefusesSilentlyWrongInput:
    def test_an_unknown_account(self, db):
        with pytest.raises(CommandError, match="no account matches"):
            _run(account="nobody@example.com", credits=10, reason="r", actor="a")
        assert Transaction.objects.count() == 0

    def test_an_unknown_id(self, db):
        with pytest.raises(CommandError, match="no account"):
            _run(account="00000000-0000-0000-0000-000000000000",
                 credits=10, reason="r", actor="a")
        assert Transaction.objects.count() == 0

    def test_a_negative_amount(self, user):
        with pytest.raises(CommandError, match="must be positive"):
            _run(account=str(user.pk), credits=-50, reason="r", actor="a")
        assert Transaction.objects.count() == 0

    def test_zero(self, user):
        with pytest.raises(CommandError, match="must be positive"):
            _run(account=str(user.pk), credits=0, reason="r", actor="a")
        assert Transaction.objects.count() == 0

    def test_an_empty_reason(self, user):
        with pytest.raises(CommandError, match="reason"):
            _run(account=str(user.pk), credits=10, reason="   ", actor="a")
        assert Transaction.objects.count() == 0

    def test_an_empty_actor(self, user):
        with pytest.raises(CommandError, match="actor"):
            _run(account=str(user.pk), credits=10, reason="r", actor=" ")
        assert Transaction.objects.count() == 0

    def test_an_ambiguous_email(self, user):
        from django.contrib.auth import get_user_model

        get_user_model().objects.create_user(
            username="twin", email="TESTUSER@example.com", password="x12345678"
        )
        with pytest.raises(CommandError, match="more than one account"):
            _run(account="testuser@example.com", credits=10, reason="r", actor="a")
        assert Transaction.objects.count() == 0


@pytest.mark.django_db
class TestTheServiceUnderneath:
    def test_resolve_account_refuses_an_empty_reference(self, db):
        with pytest.raises(services.AccountNotFoundError):
            services.resolve_account("")

    def test_grant_credits_settles_an_outstanding_debt(self, user):
        services.debit(
            user=user, credits=30, type=TransactionType.AI_CHARGE, allow_partial=True
        )
        services.grant_credits(user=user, credits=50, reason="make good", actor="ops")
        wallet = services.get_or_create_wallet(user)
        wallet.refresh_from_db()
        # A top-up on an indebted wallet is repayment before it is spending.
        assert wallet.balance == 20
