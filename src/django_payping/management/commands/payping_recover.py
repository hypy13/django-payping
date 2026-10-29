from uuid import UUID

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db.models import F
from django.utils import timezone

from django_payping.events import deliver_pending_events
from django_payping.exceptions import PayPingError
from django_payping.models import PayPingPayment
from django_payping.services import reconcile_payment


class Command(BaseCommand):
    help = "Reconcile unfinished payments and retry undelivered integration events."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--events-only", action="store_true")
        parser.add_argument(
            "--payment", type=UUID, help="Reconcile one local payment UUID."
        )

    def handle(self, *args, **options) -> None:
        limit = options["limit"]
        if limit <= 0:
            raise CommandError("--limit must be positive.")
        if not options["events_only"]:
            payments = (
                PayPingPayment.objects.filter(
                    status__in={"pending", "processing", "canceled", "failed"},
                    payment_code__isnull=False,
                )
                .exclude(payment_code="")
                .order_by(F("last_reconciled_at").asc(nulls_first=True), "created_at")
            )
            if options["payment"]:
                payments = payments.filter(pk=options["payment"])
            for payment in payments[:limit]:
                try:
                    result = reconcile_payment(payment.pk)
                    self.stdout.write(f"{payment.pk}: {result.status}")
                except PayPingError as exc:
                    self.stderr.write(f"{payment.pk}: {exc}")
                finally:
                    PayPingPayment.objects.filter(pk=payment.pk).update(
                        last_reconciled_at=timezone.now()
                    )
        count = deliver_pending_events(limit=limit)
        self.stdout.write(f"Delivered {count} pending events.")
