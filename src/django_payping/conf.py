from typing import TYPE_CHECKING, Any, cast

from django.conf import settings
from django.utils.module_loading import import_string

from django_payping.client import PayPingClient
from django_payping.exceptions import PayPingConfigurationError
from django_payping.types import ClientFactory, PaymentGateway

if TYPE_CHECKING:
    from django_payping.models import PayPingPayment


def configured_callable(name: str) -> Any:
    """Resolve an optional, dynamically configured host integration hook."""
    value = getattr(settings, name, None)
    if isinstance(value, str):
        try:
            value = import_string(value)
        except ImportError as exc:
            raise PayPingConfigurationError(f"{name} could not be imported.") from exc
    if value is not None and not callable(value):
        raise PayPingConfigurationError(f"{name} must be a callable or an import path.")
    return value


def get_client(payment: "PayPingPayment") -> PaymentGateway:
    """Use the same payment-aware factory for every provider operation."""
    factory = cast(ClientFactory | None, configured_callable("PAYPING_CLIENT_FACTORY"))
    if factory:
        return factory(payment)
    return PayPingClient(base_url=payment.gateway_base_url or None)
