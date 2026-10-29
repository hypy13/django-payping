from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from datetime import timedelta
from functools import partial
from uuid import UUID

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from django_payping.conf import get_client
from django_payping.constants import MAX_AMOUNT_TOMAN, MAX_PAYMENT_REFERENCE
from django_payping.events import deliver_event, record_event
from django_payping.exceptions import (
    PaymentAlreadyProcessing,
    PaymentCallbackError,
    PaymentCreationUncertain,
    PaymentVerificationPending,
    PayPingAPIError,
    PayPingError,
    PayPingNetworkError,
    PayPingResponseError,
)
from django_payping.models import PayPingEvent, PayPingPayment
from django_payping.types import (
    CreatePaymentPayload,
    JsonValue,
    PaymentCallback,
    PaymentGateway,
    PaymentRequest,
)
from django_payping.validation import integer_value

VERIFY_STATES = frozenset(
    {
        PayPingPayment.Status.PENDING,
        PayPingPayment.Status.PROCESSING,
        PayPingPayment.Status.CANCELED,
        PayPingPayment.Status.FAILED,
    }
)


class PaymentService:
    """Coordinate payment persistence, provider operations, and durable events.

    An injected client is reused for this service instance. Without one, each
    operation resolves the configured payment-aware client factory.
    """

    def __init__(self, *, client: PaymentGateway | None = None) -> None:
        self._client = client

    def create(self, data: PaymentRequest) -> PayPingPayment:
        """Create an amount snapshot, or reuse the same business attempt.

        Raises:
            ValueError: Invalid input or reuse of a key with different payment data.
        """
        amount_toman = data.amount_toman
        order_reference = data.order_reference
        description = data.description
        payer_name = data.payer_name
        payer_identity = data.payer_identity
        metadata = data.metadata
        idempotency_key = data.idempotency_key
        account = data.account
        is_reversible = data.is_reversible
        is_blocked = data.is_blocked
        if type(amount_toman) is not int or not 0 < amount_toman <= MAX_AMOUNT_TOMAN:
            raise ValueError("amount_toman must be a positive int32 in toman.")
        if type(is_reversible) is not bool or type(is_blocked) is not bool:
            raise ValueError("Payment flags must be booleans.")
        values: dict[str, object] = {
            "order_reference": order_reference,
            "description": description,
            "payer_name": payer_name,
            "payer_identity": payer_identity,
            "account": account,
        }
        for name, value in values.items():
            model_field = PayPingPayment._meta.get_field(name)
            assert isinstance(model_field, models.CharField)
            limit = model_field.max_length
            assert limit is not None
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(
                    f"{name} must be a string of at most {limit} characters."
                )
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or len(idempotency_key) > 200
        ):
            raise ValueError("idempotency_key must contain 1 to 200 characters.")
        values.update(
            amount_toman=amount_toman,
            metadata=_metadata_snapshot(metadata),
            is_reversible=is_reversible,
            is_blocked=is_blocked,
        )
        with transaction.atomic():
            if idempotency_key is None:
                payment = PayPingPayment.objects.create(**values)
                created = True
            else:
                payment, created = PayPingPayment.objects.get_or_create(
                    idempotency_key=idempotency_key,
                    defaults=values,
                )
                if any(getattr(payment, key) != value for key, value in values.items()):
                    raise ValueError(
                        "This idempotency key belongs to different payment data."
                    )
            if created:
                record_event(payment, PayPingEvent.Kind.CREATED, once=True)
        return payment

    def initiate_payment(
        self,
        payment_id: UUID,
        *,
        return_url: str,
        client: PaymentGateway | None = None,
    ) -> str:
        """Create a provider instruction once; ambiguous outcomes require reconciliation.

        Raises:
            PaymentAlreadyProcessing: Another caller holds the creation claim.
            PaymentCreationUncertain: A previous instruction may exist remotely.
            PaymentVerificationPending: The payment is being verified.
            PaymentCallbackError: The payment cannot be started in its current state.
            PayPingError: Provider creation failed.
        """
        payment = PayPingPayment.objects.get(pk=payment_id)
        if payment.status == PayPingPayment.Status.PENDING and payment.payment_code:
            return payment.redirect_url
        if payment.status == PayPingPayment.Status.PROCESSING:
            raise PaymentVerificationPending(
                "Verification is pending; do not pay again."
            )
        if payment.status == PayPingPayment.Status.UNCERTAIN:
            raise PaymentCreationUncertain(
                "Reconcile this instruction before creating another."
            )
        if payment.status in {
            PayPingPayment.Status.SUCCEEDED,
            PayPingPayment.Status.FAILED,
            PayPingPayment.Status.CANCELED,
        }:
            raise PaymentCallbackError(
                "Create a distinct attempt only after resolving this payment."
            )
        client = client or self._client or get_client(payment)
        now = timezone.now()
        if payment.status == PayPingPayment.Status.CREATING:
            if now - payment.updated_at <= timedelta(
                seconds=max(float(client.timeout) * 2, 60)
            ):
                raise PaymentAlreadyProcessing(
                    "Payment creation is already in progress."
                )
            self._transition(
                payment.pk,
                {PayPingPayment.Status.CREATING},
                PayPingPayment.Status.UNCERTAIN,
                PayPingEvent.Kind.CREATION_FAILED,
                error_message="Creation worker expired; provider outcome is unknown.",
            )
            raise PaymentCreationUncertain(
                "Creation worker expired; reconcile before retrying."
            )
        claimed = PayPingPayment.objects.filter(
            pk=payment.pk, status=PayPingPayment.Status.NEW
        ).update(
            status=PayPingPayment.Status.CREATING,
            gateway_base_url=client.base_url,
            updated_at=now,
        )
        if not claimed:
            raise PaymentAlreadyProcessing("Payment creation is already in progress.")
        payload: CreatePaymentPayload = {
            "PayerName": payment.payer_name,
            "Amount": payment.amount_toman,
            "PayerIdentity": payment.payer_identity,
            "ReturnUrl": return_url,
            "Description": payment.description,
            "ClientRefId": str(payment.client_ref_id),
            "isReversible": payment.is_reversible,
            "IsBlocked": payment.is_blocked,
        }
        try:
            response = client.create_payment(payload)
            code = response.get("paymentCode")
            if not isinstance(code, str) or not code.strip() or len(code) > 100:
                raise PayPingResponseError("Invalid paymentCode in creation response.")
        except Exception as exc:
            # A transport error, invalid success body or server error can occur after
            # the provider created an instruction. Never blindly send it again.
            definitive = (
                isinstance(exc, PayPingAPIError) and 400 <= exc.status_code < 500
            )
            self._transition(
                payment.pk,
                {PayPingPayment.Status.CREATING},
                PayPingPayment.Status.NEW
                if definitive
                else PayPingPayment.Status.UNCERTAIN,
                PayPingEvent.Kind.CREATION_FAILED,
                error_message=str(exc)[:500],
                trace_id=getattr(exc, "trace_id", ""),
            )
            raise
        payment = self._transition(
            payment.pk,
            {PayPingPayment.Status.CREATING, PayPingPayment.Status.UNCERTAIN},
            PayPingPayment.Status.PENDING,
            PayPingEvent.Kind.INITIATED,
            payment_code=code,
            creation_response=response,
            initiated_at=timezone.now(),
            error_message="",
            trace_id="",
        )
        return payment.redirect_url

    def verify_callback(
        self, data: PaymentCallback, *, client: PaymentGateway | None = None
    ) -> PayPingPayment:
        """Verify identity and amount with PayPing, allowing late authoritative success.

        Raises:
            PaymentCallbackError: Callback or provider identity does not match.
            PaymentVerificationPending: Verification must be retried.
            PayPingError: Provider verification failed.
        """
        client_ref_id = data.client_ref_id
        payment_code = data.payment_code
        payment_ref_id = data.payment_ref_id
        amount_toman = data.amount_toman
        if type(amount_toman) is not int or amount_toman <= 0:
            raise PaymentCallbackError("Callback amount must be a positive integer.")
        reference_number = integer_value(payment_ref_id)
        if (
            reference_number is None
            or not 0 < reference_number <= MAX_PAYMENT_REFERENCE
        ):
            raise PaymentCallbackError("Invalid payment reference.")
        reference = str(reference_number)
        payment = self.get_callback_payment(
            client_ref_id, payment_code, amount_toman=amount_toman
        )
        if payment.payment_ref_id and payment.payment_ref_id != reference:
            raise PaymentCallbackError("Transaction reference conflict.")
        if payment.status == PayPingPayment.Status.SUCCEEDED:
            if payment.payment_ref_id != reference:
                raise PaymentCallbackError(
                    "Transaction reference differs from the successful payment."
                )
            with transaction.atomic():
                record_event(payment, PayPingEvent.Kind.DUPLICATE, once=True)
                self._retry_events_after_commit(payment)
            return payment
        client = client or self._client or get_client(payment)
        # Persist the reference before I/O so a worker can retry after a process crash.
        # PROCESSING prevents the same local attempt being sent back to the bank.
        self._transition(
            payment.pk,
            VERIFY_STATES,
            PayPingPayment.Status.PROCESSING,
            PayPingEvent.Kind.PROCESSING,
            payment_ref_id=reference,
            error_message="",
        )
        try:
            verified = client.verify_payment(
                payment_ref_id=reference,
                payment_code=payment_code,
                amount_toman=payment.amount_toman,
            )
        except PayPingError as exc:
            current = PayPingPayment.objects.get(pk=payment.pk)
            if current.status == PayPingPayment.Status.SUCCEEDED:
                if current.payment_ref_id != reference:
                    raise PaymentCallbackError(
                        "Transaction reference conflict."
                    ) from exc
                return current
            # HTTP status alone does not prove that a charge failed. Preserve
            # processing for all ambiguous failures, including network interruptions.
            terminal = isinstance(exc, PayPingAPIError) and exc.error_code in {
                121,
                122,
                127,
                131,
            }
            self._transition(
                payment.pk,
                {PayPingPayment.Status.PROCESSING},
                PayPingPayment.Status.FAILED
                if terminal
                else PayPingPayment.Status.PROCESSING,
                PayPingEvent.Kind.FAILED if terminal else PayPingEvent.Kind.PROCESSING,
                error_message=str(exc)[:500],
                trace_id=getattr(exc, "trace_id", ""),
            )
            if isinstance(exc, (PaymentVerificationPending, PayPingNetworkError)) or (
                isinstance(exc, PayPingAPIError) and not terminal
            ):
                raise PaymentVerificationPending(
                    "Verification needs retry or reconciliation."
                ) from exc
            raise
        if str(verified.get("code", "")) != payment.payment_code:
            raise PaymentCallbackError("Verified payment code does not match.")
        if integer_value(verified.get("amount")) != payment.amount_toman:
            raise PaymentCallbackError("Verified amount does not match.")
        # 409/110 is the documented exception: there is no success body to compare.
        if verified.get("duplicate") is not True:
            if str(verified.get("clientRefId", "")) != str(payment.client_ref_id):
                raise PaymentCallbackError("Verified client reference does not match.")
            if "paymentRefId" in verified and integer_value(
                verified["paymentRefId"]
            ) != int(reference):
                raise PaymentCallbackError(
                    "Verified transaction reference does not match."
                )
        with transaction.atomic():
            current = PayPingPayment.objects.select_for_update().get(pk=payment.pk)
            if current.status == PayPingPayment.Status.SUCCEEDED:
                if current.payment_ref_id != reference:
                    raise PaymentCallbackError("Transaction reference conflict.")
                record_event(current, PayPingEvent.Kind.DUPLICATE, once=True)
                self._retry_events_after_commit(current)
                return current
            if current.status not in VERIFY_STATES:
                raise PaymentCallbackError("Payment is not verifiable.")
            current.status = PayPingPayment.Status.SUCCEEDED
            current.payment_ref_id = reference
            current.verification_response = verified
            current.card_number = str(verified.get("cardNumber") or "")[:100]
            current.card_hash_pan = str(verified.get("cardHashPan") or "")[:200]
            current.error_message = ""
            current.trace_id = ""
            current.paid_at = timezone.now()
            current.save(
                update_fields={
                    "status",
                    "payment_ref_id",
                    "verification_response",
                    "card_number",
                    "card_hash_pan",
                    "error_message",
                    "trace_id",
                    "paid_at",
                }
            )
            record_event(current, PayPingEvent.Kind.SUCCEEDED, once=True)
            if verified.get("duplicate") is True:
                record_event(current, PayPingEvent.Kind.DUPLICATE, once=True)
        return current

    def cancel_payment(
        self, *, client_ref_id: str, payment_code: str = ""
    ) -> PayPingPayment:
        """Record advisory browser cancellation; provider success can recover it.

        Raises:
            PaymentCallbackError: The callback does not identify an existing payment.
        """
        payment = self.get_callback_payment(
            client_ref_id, payment_code, require_code=False
        )
        # Never let an untrusted cancellation interrupt verification or downgrade success.
        return self._transition(
            payment.pk,
            {PayPingPayment.Status.PENDING},
            PayPingPayment.Status.CANCELED,
            PayPingEvent.Kind.CANCELED,
        )

    def fail_payment(
        self, payment_id: UUID, *, error_message: str, trace_id: str = ""
    ) -> PayPingPayment:
        """Record a failure without downgrading a successful payment."""
        return self._transition(
            payment_id,
            VERIFY_STATES,
            PayPingPayment.Status.FAILED,
            PayPingEvent.Kind.FAILED,
            error_message=str(error_message)[:500],
            trace_id=str(trace_id)[:100],
        )

    def get_callback_payment(
        self,
        client_ref_id: str,
        payment_code: str,
        *,
        amount_toman: int | None = None,
        require_code: bool = True,
    ) -> PayPingPayment:
        """Resolve and validate browser-supplied identity before provider I/O.

        Raises:
            PaymentCallbackError: Identity, amount, or local state is invalid.
        """
        try:
            reference = UUID(str(client_ref_id))
            payment = PayPingPayment.objects.get(client_ref_id=reference)
        except (
            ValueError,
            TypeError,
            AttributeError,
            PayPingPayment.DoesNotExist,
        ) as exc:
            raise PaymentCallbackError("Unknown payment reference.") from exc
        if (
            not payment.payment_code
            or (require_code and not payment_code)
            or (payment_code and payment_code != payment.payment_code)
        ):
            raise PaymentCallbackError("Callback payment code does not match.")
        if amount_toman is not None and (
            type(amount_toman) is not int or amount_toman != payment.amount_toman
        ):
            raise PaymentCallbackError("Callback amount does not match.")
        if payment.status not in VERIFY_STATES | {PayPingPayment.Status.SUCCEEDED}:
            raise PaymentCallbackError("Payment is not verifiable.")
        return payment

    def reconcile_payment(
        self, payment_id: UUID, *, client: PaymentGateway | None = None
    ) -> PayPingPayment:
        """Recover a missing callback using the documented v1 payment report.

        Raises:
            PaymentCreationUncertain: There is no provider code to look up.
            PaymentVerificationPending: Provider payment or verification is unfinished.
            PaymentCallbackError: Report identity differs from the local payment.
            PayPingError: Report lookup or verification failed.
        """
        payment = PayPingPayment.objects.get(pk=payment_id)
        if payment.status == PayPingPayment.Status.SUCCEEDED:
            with transaction.atomic():
                self._retry_events_after_commit(payment)
            return payment
        if not payment.payment_code:
            raise PaymentCreationUncertain(
                "No provider code is known; resolve with PayPing support."
            )
        client = client or self._client or get_client(payment)
        if (
            payment.payment_ref_id
            and payment.status == PayPingPayment.Status.PROCESSING
        ):
            reference = payment.payment_ref_id
        else:
            report = client.get_payment_report(payment.payment_code)
            if integer_value(report.get("amount")) != payment.amount_toman or str(
                report.get("clientRefId", "")
            ) != str(payment.client_ref_id):
                raise PaymentCallbackError(
                    "Reconciliation report identity does not match."
                )
            if (
                report.get("isPaid") is not True
                and integer_value(report.get("paymentStatus")) != 5
            ):
                raise PaymentVerificationPending(
                    "Provider has not confirmed a payable transaction."
                )
            reference = str(report.get("refId", ""))
        return self.verify_callback(
            PaymentCallback(
                client_ref_id=str(payment.client_ref_id),
                payment_code=payment.payment_code,
                payment_ref_id=reference,
                amount_toman=payment.amount_toman,
            ),
            client=client,
        )

    def recover_payment_code(
        self,
        payment_id: UUID,
        *,
        payment_code: str,
        client: PaymentGateway | None = None,
    ) -> PayPingPayment:
        """Attach a code obtained from support after an ambiguous creation outcome.

        Raises:
            PaymentCallbackError: The code or its provider report does not match.
            PayPingError: Report lookup failed.
        """
        if (
            not isinstance(payment_code, str)
            or not payment_code.strip()
            or len(payment_code) > 100
        ):
            raise PaymentCallbackError("Invalid recovery payment code.")
        payment = PayPingPayment.objects.get(pk=payment_id)
        if payment.status != PayPingPayment.Status.UNCERTAIN:
            raise PaymentCallbackError(
                "Only uncertain creation can attach a recovered code."
            )
        client = client or self._client or get_client(payment)
        report = client.get_payment_report(payment_code)
        if integer_value(report.get("amount")) != payment.amount_toman or str(
            report.get("clientRefId", "")
        ) != str(payment.client_ref_id):
            raise PaymentCallbackError("Recovered code belongs to another payment.")
        return self._transition(
            payment.pk,
            {PayPingPayment.Status.UNCERTAIN},
            PayPingPayment.Status.PENDING,
            PayPingEvent.Kind.INITIATED,
            payment_code=payment_code,
            gateway_base_url=client.base_url,
            initiated_at=timezone.now(),
            error_message="",
        )

    def _transition(
        self,
        payment_id: UUID,
        states: Collection[str],
        status: PayPingPayment.Status,
        kind: PayPingEvent.Kind,
        **changes,
    ) -> PayPingPayment:
        with transaction.atomic():
            payment = PayPingPayment.objects.select_for_update().get(pk=payment_id)
            reference = changes.get("payment_ref_id")
            if (
                reference
                and payment.payment_ref_id
                and payment.payment_ref_id != reference
            ):
                raise PaymentCallbackError("Transaction reference conflict.")
            if payment.status not in states:
                return payment
            changed = payment.status != status
            payment.status = status
            for name, value in changes.items():
                setattr(payment, name, value)
            payment.save(update_fields={"status", *changes})
            if changed or kind == PayPingEvent.Kind.CREATION_FAILED:
                record_event(
                    payment,
                    kind,
                    payload={
                        "error_message": payment.error_message,
                        "trace_id": payment.trace_id,
                    },
                )
        return payment

    def _retry_events_after_commit(self, payment: PayPingPayment) -> None:
        if not getattr(settings, "PAYPING_AUTO_DELIVER_EVENTS", True):
            return
        for event_id in payment.events.filter(delivered_at__isnull=True).values_list(
            "pk", flat=True
        ):
            transaction.on_commit(partial(deliver_event, event_id))


def create_payment(
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
) -> PayPingPayment:
    """Compatibility entry point for PaymentService.create()."""
    return PaymentService().create(
        PaymentRequest(
            amount_toman=amount_toman,
            order_reference=order_reference,
            description=description,
            payer_name=payer_name,
            payer_identity=payer_identity,
            metadata=metadata if metadata is not None else {},
            idempotency_key=idempotency_key,
            account=account,
            is_reversible=is_reversible,
            is_blocked=is_blocked,
        ),
    )


def initiate_payment(
    payment_id: UUID,
    *,
    return_url: str,
    client: PaymentGateway | None = None,
) -> str:
    """Compatibility entry point for PaymentService.initiate_payment()."""
    return PaymentService().initiate_payment(
        payment_id=payment_id,
        return_url=return_url,
        client=client,
    )


def verify_payment_callback(
    *,
    client_ref_id: str,
    payment_code: str,
    payment_ref_id: str,
    amount_toman: int,
    client: PaymentGateway | None = None,
) -> PayPingPayment:
    """Compatibility entry point for PaymentService.verify_callback()."""
    return PaymentService().verify_callback(
        PaymentCallback(
            client_ref_id=client_ref_id,
            payment_code=payment_code,
            payment_ref_id=payment_ref_id,
            amount_toman=amount_toman,
        ),
        client=client,
    )


def cancel_payment(*, client_ref_id: str, payment_code: str = "") -> PayPingPayment:
    """Compatibility entry point for PaymentService.cancel_payment()."""
    return PaymentService().cancel_payment(
        client_ref_id=client_ref_id,
        payment_code=payment_code,
    )


def fail_payment(
    payment_id: UUID, *, error_message: str, trace_id: str = ""
) -> PayPingPayment:
    """Compatibility entry point for PaymentService.fail_payment()."""
    return PaymentService().fail_payment(
        payment_id=payment_id,
        error_message=error_message,
        trace_id=trace_id,
    )


def get_callback_payment(
    client_ref_id: str,
    payment_code: str,
    *,
    amount_toman: int | None = None,
    require_code: bool = True,
) -> PayPingPayment:
    """Compatibility entry point for PaymentService.get_callback_payment()."""
    return PaymentService().get_callback_payment(
        client_ref_id=client_ref_id,
        payment_code=payment_code,
        amount_toman=amount_toman,
        require_code=require_code,
    )


def reconcile_payment(
    payment_id: UUID, *, client: PaymentGateway | None = None
) -> PayPingPayment:
    """Compatibility entry point for PaymentService.reconcile_payment()."""
    return PaymentService().reconcile_payment(
        payment_id=payment_id,
        client=client,
    )


def recover_payment_code(
    payment_id: UUID,
    *,
    payment_code: str,
    client: PaymentGateway | None = None,
) -> PayPingPayment:
    """Compatibility entry point for PaymentService.recover_payment_code()."""
    return PaymentService().recover_payment_code(
        payment_id=payment_id,
        payment_code=payment_code,
        client=client,
    )


def _metadata_snapshot(metadata: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Validate and copy JSON metadata before creating an idempotent snapshot."""
    if not isinstance(metadata, Mapping):
        raise ValueError("metadata must be a JSON object.")
    try:
        return json.loads(json.dumps(dict(metadata), allow_nan=False))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "metadata must contain finite, JSON-serializable values."
        ) from exc
