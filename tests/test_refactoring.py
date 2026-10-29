from decimal import Decimal
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import translation
from django.utils.functional import Promise

from django_payping.conf import get_client
from django_payping.exceptions import PaymentCallbackError, PayPingConfigurationError
from django_payping.models import PayPingEvent, PayPingPayment
from django_payping.money import amount_to_toman
from django_payping.services import PaymentService, create_payment
from django_payping.types import (
    CreatePaymentPayload,
    CreatePaymentResponse,
    PaymentCallback,
    PaymentReport,
    PaymentRequest,
    VerifyPaymentResponse,
)


class FakeGateway:
    """A host can inject a gateway without inheriting from PayPingClient."""

    base_url = "https://gateway.example/v3"
    timeout = 10.0

    def __init__(self):
        self.instructions: list[CreatePaymentPayload] = []
        self.verifications = 0

    def create_payment(self, payload: CreatePaymentPayload) -> CreatePaymentResponse:
        self.instructions.append(payload)
        return {"paymentCode": "provider-code", "payerWage": 30}

    def verify_payment(
        self, *, payment_ref_id: str, payment_code: str, amount_toman: int
    ) -> VerifyPaymentResponse:
        self.verifications += 1
        return {
            "code": payment_code,
            "amount": amount_toman,
            "clientRefId": self.instructions[0]["ClientRefId"],
            "paymentRefId": payment_ref_id,
            "cardNumber": "1234******5678",
        }

    def get_payment_report(self, payment_code: str) -> PaymentReport:
        return {
            "amount": self.instructions[0]["Amount"],
            "clientRefId": self.instructions[0]["ClientRefId"],
            "isPaid": True,
            "refId": "12345",
        }


class PaymentServiceTests(TestCase):
    def setUp(self):
        self.gateway = FakeGateway()
        self.service = PaymentService(client=self.gateway)

    def initiate(self):
        payment = self.service.create(PaymentRequest(amount_toman=1000))
        self.service.initiate_payment(
            payment.pk, return_url="https://merchant.example/callback/"
        )
        payment.refresh_from_db()
        return payment

    def test_typed_service_supports_injected_gateway_and_recovery(self):
        payment = self.service.create(
            PaymentRequest(amount_toman=1000, idempotency_key="shop:42:attempt:1")
        )
        for _ in range(2):
            url = self.service.initiate_payment(
                payment.pk, return_url="https://merchant.example/callback/"
            )
            self.assertEqual(url, "https://gateway.example/v3/pay/start/provider-code")
        self.assertEqual(len(self.gateway.instructions), 1)
        verified = self.service.reconcile_payment(payment.pk)
        self.assertEqual(verified.status, PayPingPayment.Status.SUCCEEDED)
        self.assertEqual(verified.payment_ref_id, "12345")
        self.assertEqual(verified.creation_response["payerWage"], 30)
        self.assertEqual(
            verified.events.filter(kind=PayPingEvent.Kind.SUCCEEDED).count(), 1
        )

    def test_metadata_is_copied_before_persistence(self):
        metadata = {"order": {"items": [1, 2]}}
        payment = self.service.create(
            PaymentRequest(amount_toman=1000, metadata=metadata)
        )
        metadata["order"]["items"].append(3)
        self.assertEqual(payment.metadata, {"order": {"items": [1, 2]}})
        payment.refresh_from_db()
        self.assertEqual(payment.metadata, {"order": {"items": [1, 2]}})

    def test_idempotency_compares_the_serialized_metadata_snapshot(self):
        # JSON converts object keys to strings; repeated input must compare equally.
        metadata = {"nested": {1: ["item"]}}
        first = create_payment(
            amount_toman=1000, metadata=metadata, idempotency_key="same"
        )
        second = create_payment(
            amount_toman=1000, metadata=metadata, idempotency_key="same"
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.metadata, second.metadata)

    def test_invalid_metadata_is_rejected_before_a_payment_is_persisted(self):
        cyclic = {}
        cyclic["self"] = cyclic
        for metadata in ([], {"value": object()}, {"value": float("nan")}, cyclic):
            with self.subTest(metadata_type=type(metadata).__name__):
                with self.assertRaises(ValueError):
                    create_payment(amount_toman=1000, metadata=metadata)
        self.assertFalse(PayPingPayment.objects.exists())
        self.assertFalse(PayPingEvent.objects.exists())

    def test_processing_reference_cannot_be_replaced_by_another_callback(self):
        payment = self.initiate()
        PayPingPayment.objects.filter(pk=payment.pk).update(
            status=PayPingPayment.Status.PROCESSING, payment_ref_id="12345"
        )
        with self.assertRaises(PaymentCallbackError):
            self.service.verify_callback(
                PaymentCallback(
                    client_ref_id=str(payment.client_ref_id),
                    payment_code=payment.payment_code,
                    payment_ref_id="67890",
                    amount_toman=1000,
                )
            )
        payment.refresh_from_db()
        self.assertEqual(payment.payment_ref_id, "12345")
        self.assertEqual(self.gateway.verifications, 0)

    def test_reference_conflict_after_initial_lookup_is_rejected_before_provider_io(
        self,
    ):
        payment = self.initiate()

        def client_factory(current):
            # Simulate another worker claiming this reference after the initial read.
            PayPingPayment.objects.filter(pk=current.pk).update(
                status=PayPingPayment.Status.PROCESSING, payment_ref_id="67890"
            )
            return self.gateway

        with override_settings(PAYPING_CLIENT_FACTORY=client_factory):
            with self.assertRaises(PaymentCallbackError):
                PaymentService().verify_callback(
                    PaymentCallback(
                        client_ref_id=str(payment.client_ref_id),
                        payment_code=payment.payment_code,
                        payment_ref_id="12345",
                        amount_toman=1000,
                    )
                )
        self.assertEqual(self.gateway.verifications, 0)

    def test_invalid_factory_setting_has_a_configuration_error(self):
        payment = self.service.create(PaymentRequest(amount_toman=1000))
        for factory in (123, "missing_payping_module.factory"):
            with override_settings(PAYPING_CLIENT_FACTORY=factory):
                with self.assertRaises(PayPingConfigurationError):
                    get_client(payment)

    def test_partial_save_tracks_modification_and_empty_update_is_a_noop(self):
        payment = self.service.create(PaymentRequest(amount_toman=1000))
        original_timestamp = payment.updated_at
        payment.description = "Changed"
        payment.save(update_fields=())
        self.assertEqual(payment.updated_at, original_timestamp)
        payment.refresh_from_db()
        self.assertEqual(payment.description, "")
        payment.description = "Changed"
        payment.save(update_fields={"description"})
        updated_timestamp = payment.updated_at
        payment.refresh_from_db()
        self.assertEqual(payment.updated_at, updated_timestamp)
        self.assertEqual(payment.description, "Changed")

    def test_model_validation_enforces_provider_amount_limits(self):
        for amount in (0, 2_147_483_648):
            with self.subTest(amount=amount):
                with self.assertRaises(ValidationError) as raised:
                    PayPingPayment(amount_toman=amount).full_clean()
                self.assertIn("amount_toman", raised.exception.message_dict)


class TranslationTests(SimpleTestCase):
    def test_all_model_field_names_are_lazily_translatable(self):
        for model in (PayPingPayment, PayPingEvent):
            for field in model._meta.fields:
                with self.subTest(model=model.__name__, field=field.name):
                    self.assertIsInstance(field.verbose_name, Promise)

    def test_persian_payment_labels_remain_available(self):
        with translation.override("fa"):
            self.assertEqual(str(PayPingPayment._meta.verbose_name), "پرداخت پی‌پینگ")
            self.assertEqual(str(PayPingPayment.Status.SUCCEEDED.label), "موفق")


class MoneyTests(SimpleTestCase):
    def test_conversion_rounds_half_up_in_supported_units(self):
        for amount, currency, expected in (
            ("5", "IRR", 1),
            (Decimal("1.5"), "IRT", 2),
            (2, "IRHT", 2000),
            (2, "IRHR", 200),
            (1, "تومان", 1),
        ):
            with self.subTest(currency=currency):
                self.assertEqual(amount_to_toman(amount, currency), expected)

    def test_invalid_or_sub_toman_amounts_raise_value_error(self):
        for amount, currency in (
            ("4", "IRR"),
            ("NaN", "IRT"),
            ("Infinity", "IRT"),
            ("1e100", "IRT"),
            (0, "IRT"),
            (True, "IRT"),
            (1, "USD"),
            (1, None),
        ):
            with self.subTest(amount=amount, currency=currency):
                with self.assertRaises(ValueError):
                    amount_to_toman(amount, currency)


class ModelMetadataMigrationTests(TransactionTestCase):
    def test_metadata_migration_preserves_payment_and_delivery_state_in_both_directions(
        self,
    ):
        old = [("django_payping", "0002_payment_recovery_and_events")]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate(old)
            apps = executor.loader.project_state(old).apps
            payment = apps.get_model("django_payping", "PayPingPayment").objects.create(
                amount_toman=1200,
                status="succeeded",
                payment_code=str(uuid4()),
                payment_ref_id="12345",
                metadata={"order": "42"},
                idempotency_key="old",
            )
            event = apps.get_model("django_payping", "PayPingEvent").objects.create(
                payment=payment,
                kind="succeeded",
                payload={"status": "succeeded"},
                attempts=2,
                last_error="Retry fulfillment",
                deduplication_key="old-event",
            )
            for target in (latest, old):
                MigrationExecutor(connection).migrate(target)
                current = PayPingPayment.objects.get(pk=payment.pk)
                current_event = PayPingEvent.objects.get(pk=event.pk)
                self.assertEqual(current.amount_toman, 1200)
                self.assertEqual(current.payment_ref_id, "12345")
                self.assertEqual(current.status, "succeeded")
                self.assertEqual(current.metadata, {"order": "42"})
                self.assertEqual(current.idempotency_key, "old")
                self.assertEqual(current_event.payment_id, payment.pk)
                self.assertEqual(current_event.payload, {"status": "succeeded"})
                self.assertEqual(current_event.attempts, 2)
                self.assertIsNone(current_event.delivered_at)
                self.assertEqual(current_event.last_error, "Retry fulfillment")
        finally:
            MigrationExecutor(connection).migrate(latest)
