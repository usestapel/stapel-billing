"""DRF views for the billing service."""

import logging

from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView
from stapel_core.django.api.errors import StapelErrorResponse, StapelResponse
from stapel_core.django.api.permissions import IsServiceRequest, IsStaffUser

from . import services
from .catalog import CREDIT_PACKAGES, PLANS
from .dto import (
    CatalogResponse,
    CheckoutResponse,
    CreditOperationResponse,
    CustomerPortalResponse,
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
from .models import StripeWebhookEvent, Subscription, Wallet
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


# ─── Mappers ─────────────────────────────────────────────────


def _wallet_to_dto(w: Wallet) -> WalletResponse:
    return WalletResponse(
        user_id=w.user_id,
        balance=w.balance,
        currency=w.currency,
        auto_recharge_enabled=w.auto_recharge_enabled,
        auto_recharge_threshold=w.auto_recharge_threshold,
        auto_recharge_package=w.auto_recharge_package,
        low_balance_alert=w.low_balance_alert,
        updated_at=w.updated_at.isoformat(),
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
class WalletView(SerializerSeamMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = WalletUpdateRequestSerializer
    response_serializer_class = WalletResponseSerializer

    @extend_schema(responses={200: WalletResponseSerializer})
    def get(self, request):
        wallet = services.get_or_create_wallet(request.user)
        response_cls = self.get_response_serializer_class()
        return StapelResponse(response_cls(_wallet_to_dto(wallet)))

    @extend_schema(
        request=WalletUpdateRequestSerializer,
        responses={200: WalletResponseSerializer},
    )
    def patch(self, request):
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
class TransactionListView(SerializerSeamMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    response_serializer_class = TransactionListResponseSerializer

    @extend_schema(responses={200: TransactionListResponseSerializer})
    def get(self, request):
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
    permission_classes = [AllowAny]
    response_serializer_class = CatalogResponseSerializer

    @extend_schema(responses={200: CatalogResponseSerializer})
    def get(self, request):
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


@extend_schema(tags=["Checkout"])
class CheckoutView(SerializerSeamMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = CheckoutRequestSerializer
    response_serializer_class = CheckoutResponseSerializer

    @extend_schema(
        request=CheckoutRequestSerializer,
        responses={200: CheckoutResponseSerializer},
    )
    def post(self, request):
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        url, session_id = services.create_checkout_session(
            user=request.user,
            package=getattr(data, "package", None),
            plan=getattr(data, "plan", None),
            success_url=data.success_url or "https://example.com/success",
            cancel_url=data.cancel_url or "https://example.com/cancel",
        )
        response_cls = self.get_response_serializer_class()
        return StapelResponse(
            response_cls(CheckoutResponse(checkout_url=url, session_id=session_id))
        )


@extend_schema(tags=["Checkout"])
class CustomerPortalView(SerializerSeamMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    response_serializer_class = CustomerPortalResponseSerializer

    @extend_schema(responses={200: CustomerPortalResponseSerializer})
    def get(self, request):
        sub = getattr(request.user, "subscription", None)
        customer_id = sub.stripe_customer_id if sub else ""
        url = services.create_customer_portal(
            customer_id=customer_id,
            return_url=request.query_params.get("return_url")
            or "https://example.com/billing",
        )
        response_cls = self.get_response_serializer_class()
        return StapelResponse(response_cls(CustomerPortalResponse(portal_url=url)))


# ─── Subscription ───────────────────────────────────────────


@extend_schema(tags=["Subscription"])
class SubscriptionView(SerializerSeamMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    response_serializer_class = SubscriptionResponseSerializer

    @extend_schema(responses={200: SubscriptionResponseSerializer})
    def get(self, request):
        sub = Subscription.objects.filter(user=request.user).first()
        if not sub:
            sub = Subscription.objects.create(user=request.user)
        response_cls = self.get_response_serializer_class()
        return StapelResponse(response_cls(_sub_to_dto(sub)))


@extend_schema(tags=["Subscription"])
class SubscriptionCancelView(SerializerSeamMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    response_serializer_class = SubscriptionResponseSerializer

    @extend_schema(responses={200: SubscriptionResponseSerializer})
    def post(self, request):
        sub = Subscription.objects.filter(user=request.user).first()
        if not sub:
            return StapelErrorResponse(404, ERR_404_SUBSCRIPTION_NOT_FOUND)
        if sub.stripe_subscription_id:
            try:
                services.cancel_provider_subscription(sub.stripe_subscription_id)
            except Exception:
                # Do NOT mark cancelled locally: the user would believe
                # the subscription is off while the provider keeps charging.
                logger.exception("Provider subscription cancel failed")
                return StapelResponse(
                    {"status": "error"},
                    status=status.HTTP_502_BAD_GATEWAY,
                )
        sub.cancelled_at = timezone.now()
        sub.save(update_fields=["cancelled_at", "updated_at"])
        response_cls = self.get_response_serializer_class()
        return StapelResponse(response_cls(_sub_to_dto(sub)))


# ─── Stripe webhook ─────────────────────────────────────────


@extend_schema(tags=["Webhooks"])
class StripeWebhookView(SerializerSeamMixin, APIView):
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
            return StapelResponse({"status": "duplicate"}, status=status.HTTP_200_OK)

        try:
            with transaction.atomic():
                # Lock the claim so a concurrent retry of the same event
                # cannot run the handler twice; the processed mark commits
                # atomically with the handler's effects.
                locked = StripeWebhookEvent.objects.select_for_update().get(pk=log.pk)
                if locked.processed_at is not None:
                    return StapelResponse(
                        {"status": "duplicate"}, status=status.HTTP_200_OK
                    )
                if event_type == "checkout.session.completed":
                    services.handle_checkout_completed(event)
                elif event_type in ("invoice.paid", "invoice.payment_succeeded"):
                    services.handle_invoice_paid(event)
                elif event_type in (
                    "customer.subscription.updated",
                    "customer.subscription.created",
                ):
                    services.handle_subscription_updated(event)
                elif event_type == "customer.subscription.deleted":
                    services.handle_subscription_deleted(event)
                else:
                    logger.info("Unhandled Stripe event %s", event_type)
                locked.processed_at = timezone.now()
                locked.error = ""
                locked.save(update_fields=["processed_at", "error"])
        except Exception as exc:
            logger.exception("Stripe webhook handler failed")
            log.error = str(exc)
            log.save(update_fields=["error"])
            return StapelResponse(
                {"status": "error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        return StapelResponse({"status": "ok"})


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
    def post(self, request):
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
