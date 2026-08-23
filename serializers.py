"""Serializers for the billing API."""

from stapel_core.django.api.errors import StapelValidationError
from stapel_core.django.api.serializers import StapelDataclassSerializer

from .catalog import CREDIT_PACKAGES_BY_SLUG, PLANS_BY_SLUG
from .dto import (
    CatalogResponse,
    CheckoutRequest,
    CheckoutResponse,
    CreditDebitRequest,
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
    WalletUpdateRequest,
)
from .errors import (
    ERR_400_AMOUNT_INVALID,
    ERR_400_INVALID_PACKAGE,
    ERR_400_INVALID_PLAN,
)
from .models import TransactionType


class CreditLotResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = CreditLotResponse


class CreditHoldResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = CreditHoldResponse


class ExpiringCreditsResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = ExpiringCreditsResponse


class WalletResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = WalletResponse


class WalletUpdateRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = WalletUpdateRequest

    def validate_auto_recharge_package(self, value):
        if value is not None and value not in CREDIT_PACKAGES_BY_SLUG:
            raise StapelValidationError(ERR_400_INVALID_PACKAGE)
        return value


class TransactionResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = TransactionResponse


class TransactionListResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = TransactionListResponse


class PackageResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = PackageResponse


class PlanResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = PlanResponse


class CatalogResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = CatalogResponse


class CheckoutRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = CheckoutRequest

    def validate(self, data):
        package = getattr(data, "package", None)
        plan = getattr(data, "plan", None)
        if (package and plan) or (not package and not plan):
            raise StapelValidationError(ERR_400_INVALID_PACKAGE)
        if package and package not in CREDIT_PACKAGES_BY_SLUG:
            raise StapelValidationError(ERR_400_INVALID_PACKAGE)
        if plan and plan not in PLANS_BY_SLUG:
            raise StapelValidationError(ERR_400_INVALID_PLAN)
        return data


class CheckoutResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = CheckoutResponse


class SubscriptionResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = SubscriptionResponse


class CustomerPortalResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = CustomerPortalResponse


class CreditDebitRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = CreditDebitRequest

    def validate_credits(self, value):
        if value <= 0:
            raise StapelValidationError(ERR_400_AMOUNT_INVALID)
        return value

    def validate_type(self, value):
        if value not in TransactionType.values:
            raise StapelValidationError(ERR_400_AMOUNT_INVALID)
        return value


class CreditOperationResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = CreditOperationResponse
