import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import cast
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from django_payping import signals
from django_payping.conf import configured_callable
from django_payping.models import PayPingEvent, PayPingPayment
from django_payping.types import EventHandler, JsonValue

logger = logging.getLogger(__name__)
EVENT_SIGNALS = {
    "created": signals.payment_created,
    "initiated": signals.payment_initiated,
    "creation_failed": signals.payment_creation_failed,
    "processing": signals.payment_processing,
    "succeeded": signals.payment_succeeded,
    "failed": signals.payment_failed,
    "canceled": signals.payment_canceled,
    "duplicate": signals.payment_duplicate,
}


def record_event(
    payment: PayPingPayment,
    kind: str,
    *,
    payload: Mapping[str, JsonValue] | None = None,
    once: bool = False,
) -> PayPingEvent:
    """Call inside the transaction that changes payment state."""
    values = {
        "payment": payment,
        "kind": kind,
        "payload": {"status": payment.status, **(payload or {})},
    }
    if once:
        event, _ = PayPingEvent.objects.get_or_create(
            deduplication_key=f"{payment.pk}:{kind}",
            defaults=values,
        )
    else:
        event = PayPingEvent.objects.create(**values)
    if getattr(settings, "PAYPING_AUTO_DELIVER_EVENTS", True):
        transaction.on_commit(lambda: deliver_event(event.pk))
    return event


def deliver_event(event_id: UUID) -> bool:
    """Deliver with a lease; failures remain available for at-least-once retry."""
    now = timezone.now()
    lease = timedelta(seconds=getattr(settings, "PAYPING_EVENT_LEASE_SECONDS", 300))
    eligible = PayPingEvent.objects.filter(
        pk=event_id, delivered_at__isnull=True
    ).filter(Q(locked_at__isnull=True) | Q(locked_at__lt=now - lease))
    if not eligible.update(locked_at=now, attempts=F("attempts") + 1):
        return False
    event = PayPingEvent.objects.select_related("payment").get(pk=event_id)
    try:
        handler = cast(
            EventHandler | None, configured_callable("PAYPING_EVENT_HANDLER")
        )
        if handler:
            handler(event)
        signal = EVENT_SIGNALS.get(event.kind)
        if signal:
            results = signal.send_robust(
                sender=PayPingPayment, payment=event.payment, event=event
            )
            failures = [
                str(result) for _, result in results if isinstance(result, Exception)
            ]
            if failures:
                raise RuntimeError(" | ".join(failures))
    except Exception as exc:
        logger.exception("PayPing event %s delivery failed", event.pk)
        PayPingEvent.objects.filter(pk=event.pk, locked_at=now).update(
            locked_at=None,
            last_error=str(exc)[:2000],
        )
        return False
    PayPingEvent.objects.filter(pk=event.pk, locked_at=now).update(
        delivered_at=timezone.now(),
        locked_at=None,
        last_error="",
    )
    return True


def deliver_pending_events(*, limit: int = 100) -> int:
    """Deliver a bounded batch, visiting events with fewer attempts first."""
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit must be a positive integer.")
    expired = timezone.now() - timedelta(
        seconds=getattr(settings, "PAYPING_EVENT_LEASE_SECONDS", 300)
    )
    event_ids = list(
        PayPingEvent.objects.filter(delivered_at__isnull=True)
        .filter(Q(locked_at__isnull=True) | Q(locked_at__lt=expired))
        .order_by("attempts", "created_at", "id")
        .values_list("pk", flat=True)[:limit]
    )
    return sum(deliver_event(event_id) for event_id in event_ids)
