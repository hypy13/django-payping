from django.contrib import admin
from django.http import HttpRequest

from django_payping.models import PayPingEvent, PayPingPayment


class PayPingEventInline(admin.TabularInline):
    model = PayPingEvent
    fields = ("kind", "created_at", "delivered_at", "attempts", "last_error", "payload")
    readonly_fields = fields
    extra = 0
    can_delete = False

    def has_add_permission(
        self, request: HttpRequest, obj: PayPingPayment | None = None
    ) -> bool:
        return False


@admin.register(PayPingPayment)
class PayPingPaymentAdmin(admin.ModelAdmin):
    inlines = (PayPingEventInline,)
    list_display = (
        "order_reference",
        "amount_toman",
        "status",
        "payment_code",
        "payment_ref_id",
        "created_at",
    )
    list_filter = ("status", "created_at")
    search_fields = (
        "order_reference",
        "client_ref_id",
        "payment_code",
        "payment_ref_id",
    )
    readonly_fields = tuple(field.name for field in PayPingPayment._meta.fields)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: PayPingPayment | None = None
    ) -> bool:
        return False
