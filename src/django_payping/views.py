import json
import logging
from typing import cast

from django.conf import settings
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseRedirect,
)
from django.shortcuts import resolve_url
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from django_payping.conf import configured_callable
from django_payping.exceptions import (
    PaymentCallbackError,
    PaymentVerificationPending,
    PayPingError,
)
from django_payping.models import PayPingPayment
from django_payping.services import (
    cancel_payment,
    get_callback_payment,
    verify_payment_callback,
)
from django_payping.types import JsonObject, PaymentOutcome, ResultHandler
from django_payping.validation import integer_value

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class PayPingCallbackView(View):
    """Override render_result or configure PAYPING_RESULT_HANDLER for host flows."""

    http_method_names = ["get", "post", "options"]

    def get(self, request: HttpRequest) -> HttpResponse:
        payment = None
        try:
            payload, status = self.parse_callback(request)
            reference = str(payload.get("clientRefId", ""))
            code = payload.get("paymentCode", "")
            if not isinstance(code, str):
                raise PaymentCallbackError("Invalid payment code.")
            if status == 0:
                payment = cancel_payment(client_ref_id=reference, payment_code=code)
                outcome: PaymentOutcome = (
                    "success"
                    if payment.status == "succeeded"
                    else (
                        "processing" if payment.status == "processing" else "canceled"
                    )
                )
                return self.render_result(request, payment, outcome)
            if status != 1:
                raise PaymentCallbackError("Invalid callback status.")
            amount = integer_value(payload.get("amount"))
            if amount is None:
                raise PaymentCallbackError("Invalid callback amount.")
            payment = get_callback_payment(reference, code, amount_toman=amount)
            payment = verify_payment_callback(
                client_ref_id=reference,
                payment_code=code,
                payment_ref_id=str(payload.get("paymentRefId", "")),
                amount_toman=amount,
            )
        except PaymentVerificationPending as exc:
            logger.info("PayPing verification pending: %s", exc)
            if payment:
                payment.refresh_from_db()
            return self.render_result(request, payment, "processing")
        except PaymentCallbackError as exc:
            logger.warning("Rejected PayPing callback: %s", exc)
            return self.render_result(request, None, "invalid")
        except PayPingError as exc:
            logger.warning("PayPing verification failed: %s", exc)
            if payment:
                payment.refresh_from_db()
            outcome = (
                "processing"
                if payment and payment.status == "processing"
                else "failure"
            )
            return self.render_result(request, payment, outcome)
        return self.render_result(request, payment, "success")

    post = get

    def parse_callback(self, request: HttpRequest) -> tuple[JsonObject, int | None]:
        envelope: JsonObject = {}
        raw: object = request.POST.get("data") or request.GET.get("data", "")
        try:
            if not raw and request.content_type == "application/json":
                envelope = json.loads(request.body)
                if not isinstance(envelope, dict):
                    raise ValueError
                raw = envelope.get("data", envelope)
            payload = (
                json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
            )
        except (TypeError, ValueError) as exc:
            raise PaymentCallbackError("Invalid callback body.") from exc
        if not isinstance(payload, dict):
            raise PaymentCallbackError("Invalid callback data.")
        status: object = envelope.get("status")
        if "status" in request.GET:
            status = request.GET["status"]
        if "status" in request.POST:
            status = request.POST["status"]
        return payload, integer_value(status)

    def render_result(
        self,
        request: HttpRequest,
        payment: PayPingPayment | None,
        outcome: PaymentOutcome,
    ) -> HttpResponse:
        handler = cast(
            ResultHandler | None, configured_callable("PAYPING_RESULT_HANDLER")
        )
        if handler:
            return handler(request=request, payment=payment, outcome=outcome)
        if outcome == "invalid":
            return HttpResponseBadRequest("Invalid PayPing callback.")
        setting_name = {
            "success": "PAYPING_SUCCESS_URL",
            "processing": "PAYPING_PROCESSING_URL",
        }.get(outcome, "PAYPING_FAILURE_URL")
        target = getattr(settings, setting_name, None)
        if target is None and outcome == "processing":
            return HttpResponse(
                "Payment verification is processing. Do not pay again.", status=202
            )
        if callable(target):
            target = target(request=request, payment=payment, outcome=outcome)
        url = resolve_url(target or "/")
        allowed_hosts = {
            request.get_host(),
            *getattr(settings, "PAYPING_ALLOWED_REDIRECT_HOSTS", []),
        }
        if not url_has_allowed_host_and_scheme(
            url, allowed_hosts=allowed_hosts, require_https=request.is_secure()
        ):
            return HttpResponseBadRequest("Payment result redirect is not allowed.")
        return HttpResponseRedirect(url)


# Retain the original function import for existing host URL configurations.
payment_callback_view = PayPingCallbackView.as_view()
