"""DRF views for the billing service.

Guest (anonymous session) stance
--------------------------------
With ``AUTH_ANONYMOUS`` on, a guest session is ``is_authenticated``, so a bare
``IsAuthenticated`` says nothing about whether guests belong on a view
(``stapel_core.adoption`` E001/W002). This module's answer follows from what
the endpoints do:

    **money is account business.** A wallet, a ledger, a subscription and a
    Stripe Checkout are all attached to a payer, and a guest session is a
    throwaway identity — it can be minted again with one unauthenticated
    request.

So every wallet/checkout/portal/subscription view denies guests. Two of them
were doing real damage before: ``WalletView`` minted a ``Wallet`` row per
anonymous session, and ``CheckoutView`` let a guest open a *real* Stripe
Checkout whose ``client_reference_id`` names an identity that will be gone by
the time the webhook arrives — a paid session nobody can be credited for.

Two views stay open, deliberately:

* ``CatalogView`` is the public price list — a pricing page is drawn before
  anyone logs in, and it exposes no user data.
* ``StripeWebhookView`` is not a user surface at all: it is signature-gated
  (``verify_webhook`` raises when unconfigured), and Stripe does not
  authenticate.

``InternalDebitView`` is service-to-service (``IsServiceRequest | IsStaffUser``)
and never reachable by a session of any kind.

The gate is added in :meth:`GuestDeniedMixin.get_permissions` rather than by
rewriting each view's ``permission_classes`` — see that class for why.
"""

import logging

from django.db import transaction
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, status
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView
from stapel_core.django.api.errors import (
    StapelErrorResponse,
    StapelResponse,
)
from stapel_core.django.api.permissions import (
    ANONYMOUS_ALLOWED,
    ANONYMOUS_DENIED,
    IsNotAnonymousUser,
    IsServiceRequest,
    IsStaffUser,
)
from stapel_core.django.openapi.schemas import StapelErrorSerializer

from . import redirects, services
from .catalog import CREDIT_PACKAGES, PLANS
from .dto import (
    CatalogResponse,
    CheckoutResponse,
    CreditHoldResponse,
    CreditLotResponse,
    CreditOperationResponse,
    CustomerPortalResponse,
    ExpiringCreditsResponse,
    PackageResponse,
    PlanResponse,
    SubscriptionResponse,
    TransactionListResponse,
    TransactionResponse,
    WalletResponse,
)
from .errors import (
    ERR_400_INVALID_STRIPE_SIGNATURE,
    ERR_400_INVALID_WEBHOOK_PAYLOAD,
    ERR_402_INSUFFICIENT_CREDITS,
    ERR_404_SUBSCRIPTION_NOT_FOUND,
    ERR_404_WALLET_NOT_FOUND,
)
from .models import (
    CreditHold,
    CreditLot,
    StripeWebhookEvent,
    Subscription,
    Wallet,
)
from .providers.base import ProviderNotConfiguredError
from .webhooks import get_stripe_handler
from .serializers import (
    CatalogResponseSerializer,
    CheckoutRequestSerializer,
    CheckoutResponseSerializer,
    CreditDebitRequestSerializer,
    CreditOperationResponseSerializer,
    CustomerPortalResponseSerializer,
    SubscriptionResponseSerializer,
    TransactionListResponseSerializer,
    WalletResponseSerializer,
    WalletUpdateRequestSerializer,
)

logger = logging.getLogger(__name__)


def _provider_unavailable(operation: str):
    """Answer for a payment provider that cannot serve the request.

    502, never a 200 with a fabricated payload: the client must not be told a
    payment is under way when no payment system was reached. Same answer the
    subscription cancel has always given when the provider call failed.
    """
    logger.error("Payment provider cannot serve %s", operation)
    return StapelResponse(  # noqa: R006
        {"status": "error"}, status=status.HTTP_502_BAD_GATEWAY
    )


# ─── Serializer seams ────────────────────────────────────────


class SerializerSeamMixin:
    """Overridable serializer seam for every billing APIView.

    Host projects can swap the request/response serializer of any view by
    subclassing and setting ``request_serializer_class`` /
    ``response_serializer_class`` (or overriding the getters for
    per-request decisions) — no need to rewrite the HTTP method bodies.
    """

    request_serializer_class = None
    response_serializer_class = None

    def get_request_serializer_class(self):
        return self.request_serializer_class

    def get_response_serializer_class(self):
        return self.response_serializer_class


class GuestDeniedMixin:
    """Deny an anonymous (guest) session — see the module docstring.

    The gate is appended in :meth:`get_permissions` instead of being written
    into ``permission_classes`` because that attribute is not only a gate
    here: ``PermissionAwareAutoSchema`` renders its class names into every
    operation's OpenAPI description, and ``docs/schema.json`` is gated
    byte-for-byte against the monolith aggregate's billing slice, which is
    regenerated in another repository. Enforcement must not wait for that, so
    it lands in the permission layer either way — DRF calls
    ``get_permissions()`` for every request — and ``stapel_anonymous_access``
    keeps the stance readable from the class header, which is exactly the
    case ``ANONYMOUS_DENIED`` names ("the gate lives elsewhere than
    ``permission_classes``"). Moving the class into the attribute is a
    one-line follow-up once the aggregate is regenerated.
    """

    stapel_anonymous_access = ANONYMOUS_DENIED

    def get_permissions(self):
        return [*super().get_permissions(), IsNotAnonymousUser()]


# ─── Mappers ─────────────────────────────────────────────────


def _lot_to_dto(lot: CreditLot) -> CreditLotResponse:
    return CreditLotResponse(
        id=lot.id,
        credits_remaining=lot.credits_remaining,
        credits_initial=lot.credits_initial,
        source=lot.source,
        expires_at=lot.expires_at.isoformat() if lot.expires_at else None,
        created_at=lot.created_at.isoformat(),
    )


def _hold_to_dto(h: CreditHold) -> CreditHoldResponse:
    return CreditHoldResponse(
        id=h.id,
        credits=h.credits,
        type=h.type,
        description=h.description,
        status=h.status,
        expires_at=h.expires_at.isoformat() if h.expires_at else None,
        created_at=h.created_at.isoformat(),
    )


def _wallet_to_dto(w: Wallet) -> WalletResponse:
    """The wallet snapshot: the balance *and* what it is made of.

    The lots go out in the debit walker's own order (expiring soonest,
    non-expiring last), so ``lots[0]`` is literally the next credit to be
    spent and ``expiring_soon`` is the first dated lot in that same list —
    the server answers "what expires next", instead of shipping an unordered
    set and letting every client re-derive the rule.
    """
    lots = list(services.live_lots(w))
    earliest = next((lot for lot in lots if lot.expires_at is not None), None)
    return WalletResponse(
        user_id=w.user_id,
        balance=w.balance,
        currency=w.currency,
        auto_recharge_enabled=w.auto_recharge_enabled,
        auto_recharge_threshold=w.auto_recharge_threshold,
        auto_recharge_package=w.auto_recharge_package,
        low_balance_alert=w.low_balance_alert,
        updated_at=w.updated_at.isoformat(),
        lots=[_lot_to_dto(lot) for lot in lots],
        holds=[_hold_to_dto(h) for h in services.open_holds(w)],
        expiring_soon=(
            ExpiringCreditsResponse(
                credits=earliest.credits_remaining,
                expires_at=earliest.expires_at.isoformat(),
            )
            if earliest is not None
            else None
        ),
    )


def _txn_to_dto(t) -> TransactionResponse:
    return TransactionResponse(
        id=t.id,
        type=t.type,
        amount_cents=t.amount_cents,
        credits_delta=t.credits_delta,
        balance_after=t.balance_after,
        description=t.description,
        metadata=t.metadata or {},
        created_at=t.created_at.isoformat(),
    )


def _sub_to_dto(s: Subscription) -> SubscriptionResponse:
    return SubscriptionResponse(
        plan=s.plan,
        status=s.status,
        stripe_subscription_id=s.stripe_subscription_id,
        current_period_start=s.current_period_start.isoformat()
        if s.current_period_start
        else None,
        current_period_end=s.current_period_end.isoformat()
        if s.current_period_end
        else None,
        cancelled_at=s.cancelled_at.isoformat() if s.cancelled_at else None,
    )


# ─── Wallet ──────────────────────────────────────────────────


@extend_schema(tags=["Wallet"])
class WalletView(SerializerSeamMixin, GuestDeniedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = WalletUpdateRequestSerializer
    response_serializer_class = WalletResponseSerializer

    @extend_schema(responses={200: WalletResponseSerializer})
    def get(self, request):  # noqa: R007
        wallet = services.get_or_create_wallet(request.user)
        response_cls = self.get_response_serializer_class()
        return StapelResponse(response_cls(_wallet_to_dto(wallet)))

    @extend_schema(
        request=WalletUpdateRequestSerializer,
        responses={200: WalletResponseSerializer},
    )
    def patch(self, request):  # noqa: R007
        wallet = services.get_or_create_wallet(request.user)
        ser = self.get_request_serializer_class()(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        if getattr(data, "auto_recharge_enabled", None) is not None:
            wallet.auto_recharge_enabled = data.auto_recharge_enabled
        if getattr(data, "auto_recharge_threshold", None) is not None:
            wallet.auto_recharge_threshold = data.auto_recharge_threshold
        if getattr(data, "auto_recharge_package", None) is not None:
            wallet.auto_recharge_package = data.auto_recharge_package
        if getattr(data, "low_balance_alert", None) is not None:
            wallet.low_balance_alert = data.low_balance_alert
        wallet.save()
        response_cls = self.get_response_serializer_class()
        return StapelResponse(response_cls(_wallet_to_dto(wallet)))


@extend_schema(tags=["Wallet"])
class TransactionListView(SerializerSeamMixin, GuestDeniedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    response_serializer_class = TransactionListResponseSerializer

    @extend_schema(responses={200: TransactionListResponseSerializer})
    def get(self, request):  # noqa: R007
        wallet = services.get_or_create_wallet(request.user)
        qs = wallet.transactions.order_by("-created_at")[:100]
        response_cls = self.get_response_serializer_class()
        return StapelResponse(
            response_cls(
                TransactionListResponse(
                    transactions=[_txn_to_dto(t) for t in qs],
                    has_more=False,
                )
            )
        )


# ─── Catalogue ──────────────────────────────────────────────


@extend_schema(tags=["Catalog"])
class CatalogView(SerializerSeamMixin, APIView):
    # The public price list: a pricing page is drawn before anyone logs in.
    stapel_anonymous_access = ANONYMOUS_ALLOWED
    permission_classes = [AllowAny]
    response_serializer_class = CatalogResponseSerializer

    @extend_schema(responses={200: CatalogResponseSerializer})
    def get(self, request):  # noqa: R007
        packages = [
            PackageResponse(
                slug=p.slug,
                name=p.name,
                credits=p.credits,
                price_cents=p.price_cents,
                currency=p.currency,
            )
            for p in CREDIT_PACKAGES
        ]
        plans = [
            PlanResponse(
                slug=p.slug,
                name=p.name,
                price_cents=p.price_cents,
                currency=p.currency,
                monthly_credits_included=p.monthly_credits_included,
                storage_limit_bytes=p.storage_limit_bytes,
                description=p.description,
            )
            for p in PLANS
        ]
        response_cls = self.get_response_serializer_class()
        return StapelResponse(
            response_cls(CatalogResponse(packages=packages, plans=plans))
        )


# ─── Checkout / portal ──────────────────────────────────────


#: Resolve + allowlist a redirect target. Lives in ``redirects.py`` so the
#: rule is one implementation rather than one per endpoint.
_redirect_url = redirects.resolve


@extend_schema(tags=["Checkout"])
class CheckoutView(SerializerSeamMixin, GuestDeniedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = CheckoutRequestSerializer
    response_serializer_class = CheckoutResponseSerializer

    @extend_schema(
        request=CheckoutRequestSerializer,
        responses={200: CheckoutResponseSerializer},
    )
    def post(self, request):  # noqa: R007
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        try:
            url, session_id = services.create_checkout_session(
                user=request.user,
                package=getattr(data, "package", None),
                plan=getattr(data, "plan", None),
                success_url=_redirect_url(
                    data.success_url, "CHECKOUT_SUCCESS_URL", "/billing/success"
                ),
                cancel_url=_redirect_url(
                    data.cancel_url, "CHECKOUT_CANCEL_URL", "/billing/cancel"
                ),
            )
        except ProviderNotConfiguredError:
            return _provider_unavailable("checkout")
        response_cls = self.get_response_serializer_class()
        return StapelResponse(
            response_cls(CheckoutResponse(checkout_url=url, session_id=session_id))
        )


@extend_schema(tags=["Checkout"])
class CustomerPortalView(SerializerSeamMixin, GuestDeniedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    response_serializer_class = CustomerPortalResponseSerializer

    @extend_schema(responses={200: CustomerPortalResponseSerializer})
    def get(self, request):  # noqa: R007
        sub = getattr(request.user, "subscription", None)
        customer_id = sub.stripe_customer_id if sub else ""
        try:
            url = services.create_customer_portal(
                customer_id=customer_id,
                return_url=_redirect_url(
                    request.query_params.get("return_url"),
                    "PORTAL_RETURN_URL",
                    "/billing",
                ),
            )
        except ProviderNotConfiguredError:
            return _provider_unavailable("the customer portal")
        response_cls = self.get_response_serializer_class()
        return StapelResponse(response_cls(CustomerPortalResponse(portal_url=url)))


# ─── Subscription ───────────────────────────────────────────


@extend_schema(tags=["Subscription"])
class SubscriptionView(SerializerSeamMixin, GuestDeniedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    response_serializer_class = SubscriptionResponseSerializer

    @extend_schema(responses={200: SubscriptionResponseSerializer})
    def get(self, request):  # noqa: R007
        sub = Subscription.objects.filter(user=request.user).first()
        if not sub:
            sub = Subscription.objects.create(user=request.user)
        response_cls = self.get_response_serializer_class()
        return StapelResponse(response_cls(_sub_to_dto(sub)))


@extend_schema(tags=["Subscription"])
class SubscriptionCancelView(SerializerSeamMixin, GuestDeniedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    response_serializer_class = SubscriptionResponseSerializer

    @extend_schema(
        description=(
            "Cancel the authenticated user's subscription. Cancels the "
            "subscription at the Stripe provider (when one exists) and marks "
            "it cancelled locally. Takes no request body."
        ),
        request=None,
        responses={
            200: SubscriptionResponseSerializer,
            404: StapelErrorSerializer,
            502: OpenApiTypes.OBJECT,
        },
    )
    def post(self, request):  # noqa: R007
        sub = Subscription.objects.filter(user=request.user).first()
        if not sub:
            return StapelErrorResponse(404, ERR_404_SUBSCRIPTION_NOT_FOUND)
        if sub.stripe_subscription_id:
            try:
                services.cancel_provider_subscription(sub.stripe_subscription_id)
            except Exception:
                # Do NOT mark cancelled locally: the user would believe
                # the subscription is off while the provider keeps charging.
                # An unconfigured provider raises here too (it used to return
                # quietly, which produced exactly that lie).
                logger.exception("Provider subscription cancel failed")
                return StapelResponse(  # noqa: R006
                    {"status": "error"},
                    status=status.HTTP_502_BAD_GATEWAY,
                )
        sub.cancelled_at = timezone.now()
        sub.save(update_fields=["cancelled_at", "updated_at"])
        response_cls = self.get_response_serializer_class()
        return StapelResponse(response_cls(_sub_to_dto(sub)))


# ─── Stripe webhook ─────────────────────────────────────────


@extend_schema(
    tags=["Webhooks"],
    summary="Stripe webhook receiver",
    description=(
        "External Stripe webhook endpoint. The request body is a raw Stripe "
        "Event object (JSON) whose authenticity is verified against the "
        "`Stripe-Signature` header using the configured webhook secret. "
        "Events are processed idempotently. Not called by first-party clients."
    ),
    request=OpenApiTypes.OBJECT,
    parameters=[
        OpenApiParameter(
            "Stripe-Signature",
            str,
            location=OpenApiParameter.HEADER,
            required=True,
            description=(
                "Stripe signature header used to verify the event payload."
            ),
        ),
    ],
    responses={
        200: OpenApiTypes.OBJECT,
        400: StapelErrorSerializer,
        500: OpenApiTypes.OBJECT,
    },
)
class StripeWebhookView(SerializerSeamMixin, APIView):
    """Verify, claim and dispatch one Stripe event.

    Routing is a registry, not a chain: the effective map is
    ``STAPEL_BILLING["STRIPE_WEBHOOK_HANDLERS"]`` merged over
    ``stapel_billing.webhooks.BUILTIN_STRIPE_HANDLERS`` (last-wins per
    event type), so a host adds an event type, replaces a built-in
    reaction, or switches one off with ``None`` — no fork. Everything
    around the handler stays here and cannot be opted out of: signature
    verification, the idempotency claim, the row lock and the processed
    mark. An event type with no handler is acknowledged and logged.
    """

    # Not a user surface: Stripe does not authenticate, the signature does.
    stapel_anonymous_access = ANONYMOUS_ALLOWED
    permission_classes = [AllowAny]

    def post(self, request):
        sig = request.headers.get("Stripe-Signature", "")
        try:
            event = services.verify_stripe_signature(request.body, sig)
        except ValueError:
            return StapelErrorResponse(400, ERR_400_INVALID_STRIPE_SIGNATURE)
        except Exception:
            logger.exception("Stripe webhook signature verification crashed")
            return StapelErrorResponse(400, ERR_400_INVALID_WEBHOOK_PAYLOAD)

        event_id = event.get("id")
        event_type = event.get("type")
        if not event_id or not event_type:
            return StapelErrorResponse(400, ERR_400_INVALID_WEBHOOK_PAYLOAD)

        # Idempotency: skip already-PROCESSED events. An event row without
        # processed_at is a previous failed attempt — Stripe's retry must
        # reprocess it, otherwise a paid checkout is lost forever.
        log, created = StripeWebhookEvent.objects.get_or_create(
            stripe_event_id=event_id,
            defaults={"event_type": event_type, "payload": event},
        )
        if not created and log.processed_at is not None:
            return StapelResponse({"status": "duplicate"}, status=status.HTTP_200_OK)  # noqa: R006

        try:
            with transaction.atomic():
                # Lock the claim so a concurrent retry of the same event
                # cannot run the handler twice; the processed mark commits
                # atomically with the handler's effects.
                locked = StripeWebhookEvent.objects.select_for_update().get(pk=log.pk)
                if locked.processed_at is not None:
                    return StapelResponse(  # noqa: R006
                        {"status": "duplicate"}, status=status.HTTP_200_OK
                    )
                # Routing is a registry, not an if/elif chain: a host adds
                # or replaces one event type through
                # STAPEL_BILLING["STRIPE_WEBHOOK_HANDLERS"] instead of
                # forking this view. Everything around the call — signature,
                # idempotency claim, lock, processed mark — stays here, so an
                # override cannot opt out of the guarantees that keep a paid
                # checkout from being credited twice.
                handler = get_stripe_handler(event_type)
                if handler is not None:
                    handler(event)
                else:
                    logger.info("Unhandled Stripe event %s", event_type)
                locked.processed_at = timezone.now()
                locked.error = ""
                locked.save(update_fields=["processed_at", "error"])
        except Exception as exc:
            logger.exception("Stripe webhook handler failed")
            log.error = str(exc)
            log.save(update_fields=["error"])
            return StapelResponse(  # noqa: R006
                {"status": "error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        return StapelResponse({"status": "ok"})  # noqa: R006


# ─── Internal service-to-service ────────────────────────────


@extend_schema(tags=["Internal"])
class InternalDebitView(SerializerSeamMixin, APIView):
    """Charge a user's wallet from another service (transcription/AI etc.)."""

    permission_classes = [IsServiceRequest | IsStaffUser]
    request_serializer_class = CreditDebitRequestSerializer
    response_serializer_class = CreditOperationResponseSerializer

    @extend_schema(
        request=CreditDebitRequestSerializer,
        responses={200: CreditOperationResponseSerializer},
    )
    def post(self, request):  # noqa: R007
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.filter(id=data.user_id).first()
        if not user:
            return StapelErrorResponse(404, ERR_404_WALLET_NOT_FOUND)
        try:
            txn = services.debit(
                user=user,
                credits=data.credits,
                type=data.type,
                description=data.description,
                metadata=data.metadata or {},
                idempotency_key=getattr(data, "idempotency_key", None),
            )
        except services.InsufficientCreditsError:
            return StapelErrorResponse(402, ERR_402_INSUFFICIENT_CREDITS)
        response_cls = self.get_response_serializer_class()
        return StapelResponse(
            response_cls(
                CreditOperationResponse(
                    transaction_id=txn.id, balance_after=txn.balance_after
                )
            )
        )
