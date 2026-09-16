"""Staff is not superuser, and every staff affordance must prove it separately.

THE DEFECT THIS EXISTS TO STOP RECURRING. On a live fleet, 2026-09-16, the
`Staff` group on three services had four members and ZERO permissions, and
nobody noticed for days — because every staff test anyone had written was run
as an account that was staff AND superuser. A superuser bypasses Django's
permission system entirely (`has_perm` returns True for everything), so such
an account cannot tell "the group grants this" from "the group grants nothing
and I am simply unstoppable". Two mechanisms masqueraded as one:

  * the ``is_staff`` COLUMN, which is what
    :func:`stapel_billing.internal.staff_is_internal` reads, and what decides
    whether an account is metered-but-not-charged;
  * the ``Staff`` GROUP's permissions, which is what decides whether that
    person can do anything in the admin.

The first kept working. The second was empty. The bill for the confusion was
that the admin credit-grant path — the only non-terminal way to put credits
on an account — was reachable by superusers alone, on a deployment whose
whole complaint was that staff could not get credits.

So the rule these tests pin is: **every assertion about a staff capability is
made on an account that is `is_staff=True, is_superuser=False`.** If a test
needs a superuser to pass, it is not testing what it claims.
"""

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import Group, Permission

from stapel_billing import internal, services
from stapel_billing.admin import WalletAdmin
from stapel_billing.models import Transaction, TransactionType, Wallet


@pytest.fixture
def staff_only(db):
    """staff=True, superuser=False, and NOT carrying permissions by accident."""
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(
        username="operator",
        email="operator@example.com",
        password="operatorpass123",
        is_staff=True,
    )
    assert user.is_staff is True
    assert user.is_superuser is False
    assert user.get_all_permissions() == set()
    return user


@pytest.fixture
def meter_only(settings):
    settings.STAPEL_BILLING = {"INTERNAL_ACCOUNT_POLICY": "meter_only"}
    return settings


class _Request:
    def __init__(self, post, user):
        self.POST = post
        self.user = user


@pytest.fixture
def wallet_admin():
    admin = WalletAdmin(Wallet, AdminSite())
    sent = []
    admin.message_user = lambda request, message, level=None: sent.append(
        (message, level)
    )
    admin.log_change = lambda request, obj, message: None
    admin._sent = sent
    return admin


@pytest.mark.django_db
class TestTheInternalPredicateDoesNotNeedSuperuser:
    """The half that kept working — pinned so it keeps working."""

    def test_a_staff_account_is_internal_without_being_a_superuser(
        self, meter_only, staff_only
    ):
        assert internal.is_internal(staff_only) is True
        assert internal.meter_only(staff_only) is True

    def test_it_holds_with_no_django_permissions_at_all(
        self, meter_only, staff_only
    ):
        # The exact production state: in the Staff group, group grants nothing.
        group = Group.objects.create(name="Staff")
        staff_only.groups.add(group)
        staff_only = type(staff_only).objects.get(pk=staff_only.pk)  # drop the perm cache
        assert group.permissions.count() == 0
        assert staff_only.get_all_permissions() == set()
        # The waiver is a COLUMN decision, not a permission decision.
        assert internal.meter_only(staff_only) is True

    def test_such_an_account_is_metered_at_zero_on_an_empty_wallet(
        self, meter_only, staff_only
    ):
        txn = services.debit(
            user=staff_only,
            credits=12,
            type=TransactionType.TRANSCRIPTION_CHARGE,
            allow_partial=True,
        )
        assert txn.transaction.credits_delta == 0
        assert txn.shortfall == 0
        assert internal.was_waived(txn.transaction.metadata)


@pytest.mark.django_db
class TestTheAdminGrantIsReachableWithoutSuperuser:
    """The half that was broken, and the acceptance for the fix.

    A deployment grants the Staff group its permissions from a fixture. What
    must never drift is that the grant affordance is reachable by SOMEBODY who
    is not a superuser — otherwise the only way to put credits on an account
    by hand is to hand out superuser, which is not a permission model.
    """

    def _grant(self, admin, user, wallet, **extra):
        post = {
            "grant_credits": "40",
            "grant_reason": "support goodwill",
            **extra,
        }
        admin.grant_credits(post_request := _Request(post, user), Wallet.objects.filter(pk=wallet.pk))
        return post_request

    def test_view_permission_is_enough_to_run_the_grant(
        self, wallet_admin, staff_only, user
    ):
        # Exactly the set a client fleet shipped: view, nothing else. No `change`,
        # no `add`, no `delete` — a grant must not require the right to edit
        # a wallet by hand, which is the thing the readonly_fields exist to
        # prevent.
        group = Group.objects.create(name="Staff")
        group.permissions.add(
            Permission.objects.get(
                content_type__app_label="billing",
                content_type__model="wallet",
                codename="view_wallet",
            )
        )
        staff_only.groups.add(group)
        staff_only = type(staff_only).objects.get(pk=staff_only.pk)
        assert staff_only.has_perm("billing.view_wallet")
        assert not staff_only.has_perm("billing.change_wallet")
        assert not staff_only.has_perm("billing.delete_wallet")

        wallet = services.get_or_create_wallet(user)
        self._grant(wallet_admin, staff_only, wallet)

        wallet.refresh_from_db()
        assert wallet.balance == 40
        txn = Transaction.objects.get()
        assert txn.type == TransactionType.ADJUSTMENT
        # And the ledger names the operator, not "an admin".
        assert txn.metadata["actor"] == "operator"

    def test_the_changelist_is_visible_to_that_account(
        self, wallet_admin, staff_only
    ):
        group = Group.objects.create(name="Staff")
        group.permissions.add(
            Permission.objects.get(
                content_type__app_label="billing",
                content_type__model="wallet",
                codename="view_wallet",
            )
        )
        staff_only.groups.add(group)
        staff_only = type(staff_only).objects.get(pk=staff_only.pk)
        request = _Request({}, staff_only)
        # Without this the operator lands on an admin index with nothing on
        # it, which is exactly what four real accounts saw for days.
        assert wallet_admin.has_view_permission(request) is True
        assert wallet_admin.has_delete_permission(request) is False

    def test_an_account_with_no_permissions_cannot_see_the_changelist(
        self, wallet_admin, staff_only
    ):
        # The other direction, so the test above cannot pass vacuously: being
        # staff is NOT by itself the right to look at wallets.
        request = _Request({}, staff_only)
        assert wallet_admin.has_view_permission(request) is False
