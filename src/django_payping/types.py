"""Public input objects, provider schemas, and host integration contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, TypeAlias, TypedDict

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

    from django_payping.models import PayPingEvent, PayPingPayment

JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)
JsonObject: TypeAlias = dict[str, JsonValue]
PaymentOutcome: TypeAlias = Literal[
    "success", "failure", "canceled", "processing", "invalid"
]


@dataclass(frozen=True, kw_only=True)
class PaymentRequest:
    """Server-calculated payment snapshot; amounts are integer toman.

    Validation runs when PaymentService.create() persists the request.
    Metadata must contain JSON values and is copied before persistence.
    """

    amount_toman: int
    order_reference: str = ""
    description: str = ""
    payer_name: str = ""
    payer_identity: str = ""
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)
    idempotency_key: str | None = None
    account: str = ""
    is_reversible: bool = False
    is_blocked: bool = False


@dataclass(frozen=True, kw_only=True)
class PaymentCallback:
    """Untrusted callback identity, validated before provider verification."""

    client_ref_id: str
    payment_code: str
    payment_ref_id: str
    amount_toman: int


class CreatePaymentPayload(TypedDict):
    """PayPing's wire names are intentionally distinct from Python names."""

    PayerName: str
    Amount: int
    PayerIdentity: str
    ReturnUrl: str
    Description: str
    ClientRefId: str
    isReversible: bool
    IsBlocked: bool


class PaymentAmounts(TypedDict, total=False):
    payerWage: int
    businessWage: int
    gatewayAmount: int
    paypingVat: int


class CreatePaymentResponse(PaymentAmounts):
    paymentCode: str


class VerifyPaymentResponse(PaymentAmounts, total=False):
    code: str
    amount: int | str
    clientRefId: str
    paymentRefId: int | str
    cardNumber: str | None
    cardHashPan: str | None
    duplicate: bool


class PaymentReport(TypedDict, total=False):
    amount: int | str
    clientRefId: str
    isPaid: bool
    paymentStatus: int | str
    refId: int | str


class PaymentGateway(Protocol):
    """Implement this interface to inject a client without subclassing it.

    Responses may contain additional provider metadata; clients must retain it.
    """

    @property
    def base_url(self) -> str: ...

    @property
    def timeout(self) -> float: ...

    def create_payment(
        self, payload: CreatePaymentPayload
    ) -> CreatePaymentResponse: ...

    def verify_payment(
        self, *, payment_ref_id: str, payment_code: str, amount_toman: int
    ) -> VerifyPaymentResponse: ...

    def get_payment_report(self, payment_code: str) -> PaymentReport: ...


class ClientFactory(Protocol):
    def __call__(self, payment: PayPingPayment) -> PaymentGateway: ...


class EventHandler(Protocol):
    def __call__(self, event: PayPingEvent) -> None: ...


class ResultHandler(Protocol):
    def __call__(
        self,
        *,
        request: HttpRequest,
        payment: PayPingPayment | None,
        outcome: PaymentOutcome,
    ) -> HttpResponse: ...


class ResultUrlResolver(Protocol):
    def __call__(
        self,
        *,
        request: HttpRequest,
        payment: PayPingPayment | None,
        outcome: PaymentOutcome,
    ) -> str: ...
