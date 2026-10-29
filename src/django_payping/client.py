from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import cast
from urllib.parse import quote, urlsplit

import requests
from django.conf import settings

from django_payping import __version__
from django_payping.exceptions import (
    PaymentVerificationPending,
    PayPingAPIError,
    PayPingConfigurationError,
    PayPingNetworkError,
    PayPingResponseError,
)
from django_payping.types import (
    CreatePaymentPayload,
    CreatePaymentResponse,
    JsonObject,
    PaymentReport,
    VerifyPaymentResponse,
)
from django_payping.validation import integer_value


class PayPingClient:
    """Small HTTP client for PayPing's v3 payment API."""

    def __init__(
        self,
        *,
        token: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.token: str = (
            token if token is not None else getattr(settings, "PAYPING_TOKEN", "")
        )
        configured_base_url = base_url or getattr(
            settings, "PAYPING_BASE_URL", "https://api.payping.ir/v3"
        )
        if not isinstance(configured_base_url, str) or not configured_base_url:
            raise PayPingConfigurationError("PAYPING_BASE_URL must be a URL string.")
        self.base_url = configured_base_url.rstrip("/")
        self.timeout: float = (
            timeout if timeout is not None else getattr(settings, "PAYPING_TIMEOUT", 45)
        )
        self.session = session or requests.Session()

        if not self.token:
            raise PayPingConfigurationError("PAYPING_TOKEN تنظیم نشده است.")
        if (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, (int, float))
            or not isfinite(self.timeout)
            or self.timeout <= 0
        ):
            raise PayPingConfigurationError("PAYPING_TIMEOUT must be positive.")
        parts = urlsplit(self.base_url)
        local_http = parts.scheme == "http" and parts.hostname in {
            "localhost",
            "127.0.0.1",
            "::1",
        }
        if not parts.hostname or (parts.scheme != "https" and not local_http):
            raise PayPingConfigurationError("PAYPING_BASE_URL باید HTTPS باشد.")

    def create_payment(self, payload: CreatePaymentPayload) -> CreatePaymentResponse:
        """Create a payment and return the API response."""
        response, data = self._post("/pay", payload, include_platform=True)
        if response.status_code != 200:
            raise self._api_error(response.status_code, data)

        payment_code = data.get("paymentCode")
        if not isinstance(payment_code, str) or not payment_code.strip():
            raise PayPingResponseError("پاسخ ساخت پرداخت، paymentCode معتبر ندارد.")
        if len(str(payment_code)) > 100:
            raise PayPingResponseError(
                "paymentCode در پاسخ پی‌پینگ بیش از حد طولانی است."
            )

        data["paymentCode"] = str(payment_code)
        return cast(CreatePaymentResponse, data)

    def verify_payment(
        self,
        *,
        payment_ref_id: str,
        payment_code: str,
        amount_toman: int,
    ) -> VerifyPaymentResponse:
        """Verify a payment with PayPing."""
        payload = {
            "PaymentRefId": integer_value(payment_ref_id),
            "PaymentCode": payment_code,
            "Amount": amount_toman,
        }
        response, data = self._post("/pay/verify", payload)

        # PayPing uses 409 / error code 110 when this payment was verified
        # earlier. The caller validates callback identity and amount first.
        metadata = data.get("metaData") or {}
        if not isinstance(metadata, Mapping):
            metadata = {}
        if response.status_code in {202, 502} or integer_value(
            metadata.get("code")
        ) in {109, 155}:
            raise PaymentVerificationPending(
                "PayPing verification is still processing."
            )
        if response.status_code == 409 and integer_value(metadata.get("code")) == 110:
            return {"code": payment_code, "amount": amount_toman, "duplicate": True}

        if response.status_code != 200:
            raise self._api_error(response.status_code, data)
        return cast(VerifyPaymentResponse, data)

    def get_payment_report(self, payment_code: str) -> PaymentReport:
        """Read the documented v1 reconciliation endpoint."""
        root = self.base_url.rsplit("/", 1)[0]
        try:
            response = self.session.get(
                f"{root}/v1/report/{quote(payment_code, safe='')}",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise PayPingNetworkError(
                "Could not retrieve PayPing payment report."
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise PayPingResponseError("Invalid PayPing report response.") from exc
        if not isinstance(data, dict):
            raise PayPingResponseError("Invalid PayPing report response.")
        if response.status_code != 200:
            raise self._api_error(response.status_code, data)
        return cast(PaymentReport, data)

    def _post(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        include_platform: bool = False,
    ) -> tuple[requests.Response, JsonObject]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if include_platform:
            headers.update({"X-Platform": "django", "X-Platform-Version": __version__})

        try:
            response = self.session.post(
                f"{self.base_url}{path}",
                json=dict(payload),
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise PayPingNetworkError("ارتباط با پی‌پینگ برقرار نشد.") from exc

        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        return response, data

    @staticmethod
    def _api_error(status_code: int, data: Mapping[str, object]) -> PayPingAPIError:
        metadata = data.get("metaData") or {}
        if not isinstance(metadata, Mapping):
            metadata = {}
        errors = metadata.get("errors", [])
        if not isinstance(errors, (list, tuple)):
            errors = []
        messages = [
            str(error.get("message", "")).strip()
            for error in errors
            if isinstance(error, Mapping) and error.get("message")
        ]
        if not messages and data.get("detail"):
            messages.append(str(data["detail"]).strip())

        message = " | ".join(messages) or f"خطای پی‌پینگ (HTTP {status_code})"
        return PayPingAPIError(
            message[:500],
            status_code=status_code,
            error_code=integer_value(metadata.get("code")),
            trace_id=str(data.get("paypingTraceId", ""))[:100],
        )
