import uuid

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="PayPingPayment",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "client_ref_id",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                ("order_reference", models.CharField(blank=True, db_index=True, max_length=100)),
                ("amount_toman", models.PositiveBigIntegerField()),
                ("description", models.CharField(blank=True, max_length=250)),
                ("payer_name", models.CharField(blank=True, max_length=150)),
                ("payer_identity", models.CharField(blank=True, max_length=150)),
                (
                    "payment_code",
                    models.CharField(blank=True, max_length=100, null=True, unique=True),
                ),
                ("payment_ref_id", models.CharField(blank=True, db_index=True, max_length=100)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("new", "جدید"),
                            ("creating", "در حال ساخت پرداخت"),
                            ("pending", "در انتظار پرداخت"),
                            ("succeeded", "موفق"),
                            ("failed", "ناموفق"),
                            ("canceled", "لغو شده"),
                        ],
                        default="new",
                        max_length=12,
                    ),
                ),
                ("error_message", models.CharField(blank=True, max_length=500)),
                ("trace_id", models.CharField(blank=True, max_length=100)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("initiated_at", models.DateTimeField(blank=True, null=True)),
                ("paid_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_at",
                    models.DateTimeField(default=django.utils.timezone.now, editable=False),
                ),
                ("updated_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                "db_table": "django_payping_payment",
                "verbose_name": "پرداخت پی‌پینگ",
                "verbose_name_plural": "پرداخت‌های پی‌پینگ",
                "ordering": ("-created_at",),
            },
        ),
        migrations.AddIndex(
            model_name="paypingpayment",
            index=models.Index(fields=["created_at"], name="payping_created_idx"),
        ),
        migrations.AddIndex(
            model_name="paypingpayment",
            index=models.Index(fields=["status", "created_at"], name="payping_status_created_idx"),
        ),
    ]
