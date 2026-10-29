from collections.abc import Mapping

from django.http import HttpRequest
from django.urls import reverse

from django_payping.models import PayPingPayment
from django_payping.services import create_payment, initiate_payment
from django_payping.types import JsonValue


def start_payment(
    request: HttpRequest,
    *,
    amount_toman: int,
    order_reference: str = "",
    description: str = "",
    payer_name: str = "",
    payer_identity: str = "",
    metadata: Mapping[str, JsonValue] | None = None,
    idempotency_key: str | None = None,
    account: str = "",
    is_reversible: bool = False,
    is_blocked: bool = False,
    return_url: str | None = None,
) -> PayPingPayment:
    """Convenience entry point for host checkout views; pricing stays in the host."""
    callback_url = return_url or request.build_absolute_uri(reverse("payping:callback"))
    payment = create_payment(
        amount_toman=amount_toman,
        order_reference=order_reference,
        description=description,
        payer_name=payer_name,
        payer_identity=payer_identity,
        metadata=metadata,
        idempotency_key=idempotency_key,
        account=account,
        is_reversible=is_reversible,
        is_blocked=is_blocked,
    )
    initiate_payment(payment.pk, return_url=callback_url)
    payment.refresh_from_db()
    return payment
