"""The guest (anonymous session) surface of stapel-billing.

With ``AUTH_ANONYMOUS`` on, a guest session is ``is_authenticated``, so a bare
``IsAuthenticated`` gate lets it through and nothing in the source said
whether that was wanted (``stapel_core.adoption`` E001/W002). This module had
no stance at all — while both sibling modules pin theirs here.

The answer follows from what these endpoints are: money is account business.
Two of them were doing real damage — ``WalletView`` minted a ``Wallet`` row
for every anonymous session, and ``CheckoutView`` let a guest open a real
Stripe Checkout whose ``client_reference_id`` names an identity that can be
thrown away and re-minted with one unauthenticated request.

These tests pin both facts: the door is shut for a guest, and it is shut for
*anonymous* rather than for *authenticated* — an ordinary user is unaffected,
and the public price list stays public.
"""

import pytest
from stapel_core.django.api.permissions import (
    ANONYMOUS_ALLOWED,
    ANONYMOUS_DENIED,
    IsNotAnonymousUser,
)

from stapel_billing import views
from stapel_billing.models import Subscription, Wallet

pytestmark = pytest.mark.django_db

#: Every money surface: (method, path).
MONEY_ENDPOINTS = [
    ("get", "/billing/api/wallet"),
    ("patch", "/billing/api/wallet"),
    ("get", "/billing/api/wallet/transactions"),
    ("post", "/billing/api/checkout"),
    ("get", "/billing/api/portal"),
    ("get", "/billing/api/subscription"),
    ("post", "/billing/api/subscription/cancel"),
]

MONEY_VIEWS = (
    views.WalletView,
    views.TransactionListView,
    views.CheckoutView,
    views.CustomerPortalView,
    views.SubscriptionView,
    views.SubscriptionCancelView,
)


@pytest.fixture
def guest(db):
    """A guest session's user — what ``POST /auth/api/v1/anonymous/`` mints:
    authenticated, ``is_anonymous=True``."""
    from stapel_core.django.users.models import User

    return User.create_anonymous_user()


@pytest.fixture
def guest_client(api_client, guest):
    api_client.force_authenticate(user=guest)
    return api_client


@pytest.mark.parametrize("method,path", MONEY_ENDPOINTS)
def test_guest_is_refused_at_every_money_endpoint(guest_client, method, path):
    resp = getattr(guest_client, method)(path, {}, format="json")
    assert resp.status_code == 403, (method, path, resp.content)


def test_guest_does_not_mint_a_wallet(guest_client, guest):
    """The one that wrote a row per throwaway session."""
    assert guest_client.get("/billing/api/wallet").status_code == 403
    assert not Wallet.objects.filter(user=guest).exists()


def test_guest_does_not_open_a_checkout(guest_client, stripe_placeholders_allowed):
    """A real Stripe Checkout whose buyer is a session that can be discarded
    is a payment nobody can be credited for."""
    resp = guest_client.post(
        "/billing/api/checkout", {"package": "starter"}, format="json"
    )
    assert resp.status_code == 403, resp.content


def test_guest_does_not_materialise_a_subscription(guest_client, guest):
    assert guest_client.get("/billing/api/subscription").status_code == 403
    assert not Subscription.objects.filter(user=guest).exists()


def test_an_ordinary_user_is_unaffected(authed_client, user):
    """The gate is on *anonymous*, not on *authenticated*."""
    assert authed_client.get("/billing/api/wallet").status_code == 200
    assert authed_client.get("/billing/api/wallet/transactions").status_code == 200
    assert authed_client.get("/billing/api/subscription").status_code == 200


def test_the_price_list_stays_public(guest_client, api_client):
    """Closing the money endpoints must not close the pricing page."""
    assert guest_client.get("/billing/api/products").status_code == 200
    assert api_client.get("/billing/api/products").status_code == 200


def test_every_money_view_carries_the_guest_gate():
    for view in MONEY_VIEWS:
        gates = {type(p) for p in view().get_permissions()}
        assert IsNotAnonymousUser in gates, view.__name__


def test_declarations_match_the_source():
    for view in MONEY_VIEWS:
        assert view.stapel_anonymous_access == ANONYMOUS_DENIED, view.__name__
    assert views.CatalogView.stapel_anonymous_access == ANONYMOUS_ALLOWED
    assert views.StripeWebhookView.stapel_anonymous_access == ANONYMOUS_ALLOWED


def test_no_view_is_left_silent():
    """The question ``stapel_core.adoption`` E001/W002 asks a consumer's
    deployment, asked here — where it can be answered."""
    from rest_framework.permissions import IsAuthenticated
    from rest_framework.views import APIView

    from stapel_core.django.api.permissions import ANONYMOUS_DECLARATIONS

    silent = [
        name
        for name, obj in vars(views).items()
        if isinstance(obj, type)
        and issubclass(obj, APIView)
        and set(getattr(obj, "permission_classes", ()) or ()) == {IsAuthenticated}
        and getattr(obj, "stapel_anonymous_access", None) not in ANONYMOUS_DECLARATIONS
    ]
    assert silent == []
