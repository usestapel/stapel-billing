"""Serializers for the billing API."""

from stapel_core.django.errors import IronValidationError
from stapel_core.django.serializers import IronDataclassSerializer

from .catalog import CREDIT_PACKAGES_BY_SLUG, PLANS_BY_SLUG
from .dto import (
    CatalogResponse,
    CheckoutRequest,
    CheckoutResponse,
    CreditDebitRequest,
    CreditOperationResponse,
    CustomerPortalResponse,
    PackageResponse,
    PlanResponse,
    SubscriptionResponse,
    TransactionListResponse,
    TransactionResponse,
    WalletResponse,
    WalletUpdateRequest,
)
from .errors import (
    ERR_400_AMOUNT_INVALID,
    ERR_400_INVALID_PACKAGE,
    ERR_400_INVALID_PLAN,
)
from .models import TransactionType


class WalletResponseSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = WalletResponse


class WalletUpdateRequestSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = WalletUpdateRequest

    def validate_auto_recharge_package(self, value):
        if value is not None and value not in CREDIT_PACKAGES_BY_SLUG:
            raise IronValidationError(ERR_400_INVALID_PACKAGE)
        return value


class TransactionResponseSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = TransactionResponse


class TransactionListResponseSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = TransactionListResponse


class PackageResponseSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = PackageResponse


class PlanResponseSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = PlanResponse


class CatalogResponseSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = CatalogResponse


class CheckoutRequestSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = CheckoutRequest

    def validate(self, data):
        package = getattr(data, "package", None)
        plan = getattr(data, "plan", None)
        if (package and plan) or (not package and not plan):
            raise IronValidationError(ERR_400_INVALID_PACKAGE)
        if package and package not in CREDIT_PACKAGES_BY_SLUG:
            raise IronValidationError(ERR_400_INVALID_PACKAGE)
        if plan and plan not in PLANS_BY_SLUG:
            raise IronValidationError(ERR_400_INVALID_PLAN)
        return data


class CheckoutResponseSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = CheckoutResponse


class SubscriptionResponseSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = SubscriptionResponse


class CustomerPortalResponseSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = CustomerPortalResponse


class CreditDebitRequestSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = CreditDebitRequest

    def validate_credits(self, value):
        if value <= 0:
            raise IronValidationError(ERR_400_AMOUNT_INVALID)
        return value

    def validate_type(self, value):
        if value not in TransactionType.values:
            raise IronValidationError(ERR_400_AMOUNT_INVALID)
        return value


class CreditOperationResponseSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = CreditOperationResponse
