from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from urllib.parse import quote

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from django_payping.constants import MAX_AMOUNT_TOMAN

if TYPE_CHECKING:
    from django.db.models.fields.related_descriptors import RelatedManager


class CreatedAtModel(models.Model):
    """Abstract timestamp shared by payment records and durable events."""

    created_at = models.DateTimeField(
        verbose_name=_("Created at"), default=timezone.now, editable=False
    )

    class Meta:
        abstract = True


class TimestampedModel(CreatedAtModel):
    """Update the modification timestamp on saves, including partial saves."""

    updated_at = models.DateTimeField(
        verbose_name=_("Updated at"), default=timezone.now
    )

    class Meta:
        abstract = True

    def save(self, *args, **kwargs) -> None:
        # Django treats an empty update_fields iterable as a no-op.
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = set(update_fields)
            if not update_fields:
                return
            kwargs["update_fields"] = update_fields | {"updated_at"}
        self.updated_at = timezone.now()
        super().save(*args, **kwargs)


class PayPingPayment(TimestampedModel):
    """Local payment record; the merchant's order stays in its own app."""

    class Status(models.TextChoices):
        NEW = "new", _("New")
        CREATING = "creating", _("Creating payment")
        UNCERTAIN = "uncertain", _("Creation uncertain")
        PENDING = "pending", _("Awaiting payment")
        PROCESSING = "processing", _("Verifying payment")
        SUCCEEDED = "succeeded", _("Succeeded")
        FAILED = "failed", _("Failed")
        CANCELED = "canceled", _("Canceled")

    id = models.UUIDField(
        verbose_name=_("ID"), primary_key=True, default=uuid.uuid4, editable=False
    )
    client_ref_id = models.UUIDField(
        verbose_name=_("Client reference ID"),
        default=uuid.uuid4,
        unique=True,
        editable=False,
        help_text=_("Local attempt identifier sent to PayPing as ClientRefId."),
    )
    order_reference = models.CharField(
        verbose_name=_("Order reference"), max_length=100, blank=True, db_index=True
    )
    amount_toman = models.PositiveBigIntegerField(
        verbose_name=_("Amount (toman)"),
        validators=[MinValueValidator(1), MaxValueValidator(MAX_AMOUNT_TOMAN)],
        help_text=_("Positive integer amount in toman, excluding provider fees."),
    )
    description = models.CharField(
        verbose_name=_("Description"), max_length=250, blank=True
    )
    payer_name = models.CharField(
        verbose_name=_("Payer name"), max_length=150, blank=True
    )
    payer_identity = models.CharField(
        verbose_name=_("Payer identity"), max_length=150, blank=True
    )
    payment_code = models.CharField(
        verbose_name=_("Payment code"),
        max_length=100,
        blank=True,
        null=True,
        unique=True,
    )
    payment_ref_id = models.CharField(
        verbose_name=_("Payment reference ID"),
        max_length=100,
        blank=True,
        db_index=True,
        help_text=_("Provider transaction reference returned after payment."),
    )
    status = models.CharField(
        verbose_name=_("Status"),
        max_length=12,
        choices=Status.choices,
        default=Status.NEW,
    )
    error_message = models.CharField(
        verbose_name=_("Error message"), max_length=500, blank=True
    )
    trace_id = models.CharField(verbose_name=_("Trace ID"), max_length=100, blank=True)
    metadata = models.JSONField(verbose_name=_("Metadata"), default=dict, blank=True)
    idempotency_key = models.CharField(
        verbose_name=_("Idempotency key"),
        max_length=200,
        unique=True,
        null=True,
        blank=True,
    )
    account = models.CharField(
        verbose_name=_("Merchant account"),
        max_length=100,
        blank=True,
        help_text=_("Non-secret routing key used by the configured client factory."),
    )
    gateway_base_url = models.URLField(
        verbose_name=_("Gateway base URL"), max_length=500, blank=True
    )
    is_reversible = models.BooleanField(verbose_name=_("Is reversible"), default=False)
    is_blocked = models.BooleanField(verbose_name=_("Is blocked"), default=False)
    creation_response = models.JSONField(
        verbose_name=_("Creation response"), default=dict, blank=True
    )
    verification_response = models.JSONField(
        verbose_name=_("Verification response"), default=dict, blank=True
    )
    card_number = models.CharField(
        verbose_name=_("Masked card number"), max_length=100, blank=True
    )
    card_hash_pan = models.CharField(
        verbose_name=_("Card PAN hash"), max_length=200, blank=True
    )
    initiated_at = models.DateTimeField(
        verbose_name=_("Initiated at"), null=True, blank=True
    )
    paid_at = models.DateTimeField(verbose_name=_("Paid at"), null=True, blank=True)
    last_reconciled_at = models.DateTimeField(
        verbose_name=_("Last reconciled at"), null=True, blank=True
    )

    if TYPE_CHECKING:
        events: RelatedManager[PayPingEvent]

    class Meta:
        db_table = "django_payping_payment"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("created_at",), name="payping_created_idx"),
            models.Index(
                fields=("status", "created_at"), name="payping_status_created_idx"
            ),
        ]
        verbose_name = _("PayPing payment")
        verbose_name_plural = _("PayPing payments")

    def __str__(self) -> str:
        return f"{self.order_reference or self.client_ref_id} — {self.get_status_display()}"

    @property
    def redirect_url(self) -> str:
        if not self.payment_code:
            return ""
        base_url = (
            self.gateway_base_url
            or getattr(settings, "PAYPING_BASE_URL", "https://api.payping.ir/v3")
        ).rstrip("/")
        return f"{base_url}/pay/start/{quote(self.payment_code, safe='')}"


class PayPingEvent(CreatedAtModel):
    """Durable payment timeline and at-least-once integration delivery."""

    class Kind(models.TextChoices):
        CREATED = "created", _("Created")
        INITIATED = "initiated", _("Initiated")
        CREATION_FAILED = "creation_failed", _("Creation failed")
        PROCESSING = "processing", _("Verifying payment")
        SUCCEEDED = "succeeded", _("Succeeded")
        FAILED = "failed", _("Failed")
        CANCELED = "canceled", _("Canceled")
        DUPLICATE = "duplicate", _("Duplicate callback")

    id = models.UUIDField(
        verbose_name=_("ID"), primary_key=True, default=uuid.uuid4, editable=False
    )
    payment = models.ForeignKey(
        PayPingPayment,
        verbose_name=_("Payment"),
        on_delete=models.CASCADE,
        related_name="events",
    )
    kind = models.CharField(verbose_name=_("Kind"), max_length=30, choices=Kind.choices)
    payload = models.JSONField(verbose_name=_("Payload"), default=dict, blank=True)
    deduplication_key = models.CharField(
        verbose_name=_("Deduplication key"),
        max_length=250,
        unique=True,
        null=True,
        blank=True,
    )
    delivered_at = models.DateTimeField(
        verbose_name=_("Delivered at"), null=True, blank=True
    )
    locked_at = models.DateTimeField(verbose_name=_("Locked at"), null=True, blank=True)
    attempts = models.PositiveIntegerField(
        verbose_name=_("Delivery attempts"), default=0
    )
    last_error = models.TextField(verbose_name=_("Last delivery error"), blank=True)

    class Meta:
        db_table = "django_payping_paypingevent"
        verbose_name = _("PayPing event")
        verbose_name_plural = _("PayPing events")
        ordering = ("created_at", "id")
        indexes = [
            models.Index(
                fields=("delivered_at", "created_at"), name="payping_event_pending_idx"
            )
        ]

    def __str__(self) -> str:
        return f"{self.payment_id} — {self.get_kind_display()}"
