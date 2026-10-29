import json
from datetime import timedelta
from io import StringIO
from unittest.mock import Mock
from uuid import uuid4

import requests
from django.core.management import call_command
from django.db import transaction
from django.test import RequestFactory, TransactionTestCase, override_settings
from django.utils import timezone

from django_payping.checkout import start_payment
from django_payping.client import PayPingClient
from django_payping.events import deliver_event, deliver_pending_events
from django_payping.exceptions import (
    PaymentCallbackError,
    PaymentCreationUncertain,
    PaymentVerificationPending,
    PayPingAPIError,
    PayPingNetworkError,
)
from django_payping.models import PayPingPayment
from django_payping.services import (
    cancel_payment,
    create_payment,
    initiate_payment,
    reconcile_payment,
    recover_payment_code,
    verify_payment_callback,
)
from django_payping.signals import payment_succeeded


def provider_client(*, base_url="https://api.payping.ir/v3"):
    return PayPingClient(session=Mock(), base_url=base_url)


def response(client, status, data):
    client.session.post.return_value.status_code = status
    client.session.post.return_value.json.return_value = data


class PaymentTests(TransactionTestCase):
    def pending(self, **kwargs):
        payment = create_payment(amount_toman=1000, **kwargs)
        client = provider_client()
        response(client, 200, {"paymentCode": str(uuid4()), "payerWage": 30})
        initiate_payment(
            payment.pk, return_url="https://merchant.example/callback/", client=client
        )
        payment.refresh_from_db()
        return payment

    def verification(self, payment):
        client = provider_client()
        response(
            client,
            200,
            {
                "code": payment.payment_code,
                "amount": payment.amount_toman,
                "clientRefId": str(payment.client_ref_id),
                "paymentRefId": 12345,
                "cardNumber": "1234******5678",
                "cardHashPan": "hash",
                "gatewayAmount": 1030,
            },
        )
        return client

    def verify(self, payment, client=None, **changes):
        values = dict(
            client_ref_id=str(payment.client_ref_id),
            payment_code=payment.payment_code,
            payment_ref_id="12345",
            amount_toman=1000,
            client=client or self.verification(payment),
        )
        values.update(changes)
        return verify_payment_callback(**values)

    def callback(self, payment, *, status=1, json_body=False, **changes):
        payload = dict(
            clientRefId=str(payment.client_ref_id),
            paymentCode=payment.payment_code,
            paymentRefId=12345,
            amount=1000,
        )
        payload.update(changes)
        body = dict(status=status, data=payload if json_body else json.dumps(payload))
        if json_body:
            return self.client.post(
                "/payping/callback/", json.dumps(body), content_type="application/json"
            )
        return self.client.post("/payping/callback/", body)

    def test_success_validates_and_persists_provider_details(self):
        payment = self.pending()
        result = self.verify(payment)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.card_number, "1234******5678")
        self.assertEqual(result.card_hash_pan, "hash")
        self.assertEqual(result.verification_response["gatewayAmount"], 1030)
        self.assertEqual(result.creation_response["payerWage"], 30)
        self.assertEqual(result.events.filter(kind="succeeded").count(), 1)

    def test_missing_callback_code_rejected_before_provider_call(self):
        payment = self.pending()
        client = self.verification(payment)
        with self.assertRaises(PaymentCallbackError):
            self.verify(payment, client, payment_code="")
        client.session.post.assert_not_called()

    def test_provider_identity_and_amount_must_match(self):
        for field, bad in [
            ("clientRefId", str(uuid4())),
            ("clientRefId", None),
            ("paymentRefId", 999),
            ("code", "wrong"),
            ("amount", 1000.9),
            ("amount", True),
            ("amount", "1000.0"),
        ]:
            with self.subTest(field=field, bad=bad):
                payment = self.pending()
                client = self.verification(payment)
                client.session.post.return_value.json.return_value[field] = bad
                with self.assertRaises(PaymentCallbackError):
                    self.verify(payment, client)
                payment.refresh_from_db()
                self.assertNotEqual(payment.status, "succeeded")

    def test_invalid_callback_references_rejected(self):
        payment = self.pending()
        for value in [None, True, 1.1, "123.1", "0", "-1", str(2**63), {}, "bad"]:
            with self.subTest(value=value), self.assertRaises(PaymentCallbackError):
                self.verify(payment, payment_ref_id=value)

    def test_processing_http_responses_never_succeed(self):
        for status in [202, 502]:
            for include_fields in [False, True]:
                with self.subTest(status=status, include_fields=include_fields):
                    payment = self.pending()
                    client = self.verification(payment)
                    client.session.post.return_value.status_code = status
                    if not include_fields:
                        client.session.post.return_value.json.return_value = {}
                    with self.assertRaises(PaymentVerificationPending):
                        self.verify(payment, client)
                    payment.refresh_from_db()
                    self.assertEqual(payment.status, "processing")
                    self.assertEqual(payment.payment_ref_id, "12345")
                    with self.assertRaises(PaymentVerificationPending):
                        initiate_payment(
                            payment.pk, return_url="https://merchant.example/"
                        )
                    self.assertEqual(self.verify(payment).status, "succeeded")

    def test_network_failure_retains_reference_for_recovery(self):
        payment = self.pending()
        client = self.verification(payment)
        client.session.post.side_effect = requests.Timeout()
        with self.assertRaises(PaymentVerificationPending):
            self.verify(payment, client)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "processing")
        self.assertEqual(payment.payment_ref_id, "12345")
        self.assertEqual(
            reconcile_payment(payment.pk, client=self.verification(payment)).status,
            "succeeded",
        )

    def test_http_error_alone_does_not_finalize_failure(self):
        payment = self.pending()
        client = self.verification(payment)
        response(client, 400, {"metaData": {"code": 101}})
        with self.assertRaises(PaymentVerificationPending):
            self.verify(payment, client)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "processing")

    def test_definitive_failure_is_recorded_and_late_success_can_recover(self):
        payment = self.pending()
        client = self.verification(payment)
        response(
            client, 400, {"metaData": {"code": 127}, "paypingTraceId": "support-trace"}
        )
        with self.assertRaises(PayPingAPIError):
            self.verify(payment, client)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "failed")
        self.assertEqual(payment.trace_id, "support-trace")
        self.assertEqual(payment.events.filter(kind="failed").count(), 1)
        self.assertEqual(self.verify(payment).status, "succeeded")

    def test_documented_duplicate_response_is_success(self):
        payment = self.pending()
        client = self.verification(payment)
        response(client, 409, {"metaData": {"code": 110}})
        self.assertEqual(self.verify(payment, client).status, "succeeded")
        self.assertEqual(
            client.session.post.call_args.kwargs["json"]["PaymentRefId"], 12345
        )

    def test_other_conflicts_do_not_count_as_success(self):
        payment = self.pending()
        client = self.verification(payment)
        response(client, 409, {"metaData": {"code": 126}})
        with self.assertRaises(PaymentVerificationPending):
            self.verify(payment, client)

    def test_cancellation_is_advisory(self):
        payment = self.pending()
        canceled = cancel_payment(client_ref_id=str(payment.client_ref_id))
        self.assertEqual(canceled.status, "canceled")
        result = self.verify(payment)
        self.assertEqual(result.status, "succeeded")
        result = cancel_payment(
            client_ref_id=str(payment.client_ref_id), payment_code=payment.payment_code
        )
        self.assertEqual(result.status, "succeeded")

    def test_cancellation_during_verification_cannot_win(self):
        payment = self.pending()
        client = self.verification(payment)
        result = client.session.post.return_value

        def interleave(*args, **kwargs):
            canceled = cancel_payment(client_ref_id=str(payment.client_ref_id))
            self.assertEqual(canceled.status, "processing")
            return result

        client.session.post.side_effect = interleave
        self.assertEqual(self.verify(payment, client).status, "succeeded")

    def test_success_event_once_across_duplicate_callbacks(self):
        payment = self.pending()
        receiver = Mock()
        payment_succeeded.connect(receiver, weak=False)
        try:
            self.verify(payment)
            self.verify(payment)
            self.verify(payment)
        finally:
            payment_succeeded.disconnect(receiver)
        self.assertEqual(receiver.call_count, 1)
        self.assertEqual(payment.events.filter(kind="succeeded").count(), 1)
        self.assertEqual(payment.events.filter(kind="duplicate").count(), 1)
        with self.assertRaises(PaymentCallbackError):
            self.verify(payment, payment_ref_id="999")

    def test_failed_fulfillment_is_retried_on_duplicate_callback(self):
        payment = self.pending()
        receiver = Mock(side_effect=RuntimeError("order database unavailable"))
        receiver.__qualname__ = "failing_order_receiver"
        payment_succeeded.connect(receiver, weak=False)
        try:
            with self.assertLogs("django_payping", level="ERROR"):
                self.verify(payment)
            event = payment.events.get(kind="succeeded")
            self.assertIsNone(event.delivered_at)
            self.assertIn("order database unavailable", event.last_error)
            receiver.side_effect = None
            self.verify(payment)
            event.refresh_from_db()
            self.assertIsNotNone(event.delivered_at)
            self.assertEqual(event.attempts, 2)
            self.assertEqual(receiver.call_args.kwargs["event"].pk, event.pk)
        finally:
            payment_succeeded.disconnect(receiver)

    def test_event_and_payment_transition_rollback_together(self):
        payment = self.pending()
        receiver = Mock()
        payment_succeeded.connect(receiver, weak=False)
        try:
            with self.assertRaises(RuntimeError), transaction.atomic():
                self.verify(payment)
                raise RuntimeError("host rollback")
        finally:
            payment_succeeded.disconnect(receiver)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        self.assertFalse(payment.events.filter(kind="succeeded").exists())
        receiver.assert_not_called()

    @override_settings(PAYPING_AUTO_DELIVER_EVENTS=False)
    def test_recovery_command_delivers_persisted_events(self):
        payment = self.pending()
        self.verify(payment)
        handler = Mock()
        with override_settings(PAYPING_EVENT_HANDLER=handler):
            call_command("payping_recover", events_only=True, stdout=StringIO())
        event = payment.events.get(kind="succeeded")
        event.refresh_from_db()
        self.assertIsNotNone(event.delivered_at)
        self.assertTrue(
            any(call.args[0].kind == "succeeded" for call in handler.call_args_list)
        )

    @override_settings(PAYPING_AUTO_DELIVER_EVENTS=False)
    def test_active_delivery_lease_prevents_duplicate_worker(self):
        payment = create_payment(amount_toman=1000)
        event = payment.events.get(kind="created")
        event.locked_at = timezone.now()
        event.save()
        self.assertFalse(deliver_event(event.pk))
        event.locked_at -= timedelta(minutes=10)
        event.save()
        self.assertEqual(deliver_pending_events(), 1)

    @override_settings(PAYPING_AUTO_DELIVER_EVENTS=False)
    def test_duplicate_callback_respects_worker_only_delivery(self):
        payment = self.pending()
        self.verify(payment)
        self.verify(payment)
        self.assertFalse(payment.events.filter(delivered_at__isnull=False).exists())

    @override_settings(PAYPING_AUTO_DELIVER_EVENTS=False)
    def test_failed_event_does_not_starve_other_events(self):
        first = create_payment(amount_toman=1000).events.get()
        second = create_payment(amount_toman=1000).events.get()

        def handler(event):
            if event.pk == first.pk:
                raise RuntimeError("unavailable order")

        with override_settings(PAYPING_EVENT_HANDLER=handler):
            with self.assertLogs("django_payping.events", level="ERROR"):
                self.assertEqual(deliver_pending_events(limit=1), 0)
            self.assertEqual(deliver_pending_events(limit=1), 1)
        second.refresh_from_db()
        self.assertIsNotNone(second.delivered_at)

    def test_late_creation_response_recovers_stale_worker_state(self):
        payment = create_payment(amount_toman=1000)
        client = provider_client()
        response(client, 200, {"paymentCode": str(uuid4())})
        result = client.session.post.return_value

        def interleave(*args, **kwargs):
            PayPingPayment.objects.filter(pk=payment.pk).update(status="uncertain")
            return result

        client.session.post.side_effect = interleave
        url = initiate_payment(
            payment.pk, return_url="https://merchant.example/", client=client
        )
        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        self.assertTrue(url)

    def test_business_idempotency_reuses_snapshot_and_rejects_changes(self):
        payment = create_payment(amount_toman=1000, idempotency_key="shop:42:attempt:1")
        same = create_payment(amount_toman=1000, idempotency_key="shop:42:attempt:1")
        self.assertEqual(payment.pk, same.pk)
        self.assertEqual(payment.events.count(), 1)
        with self.assertRaises(ValueError):
            create_payment(amount_toman=2000, idempotency_key="shop:42:attempt:1")
        self.assertEqual(PayPingPayment.objects.count(), 1)

    def test_create_rejects_unsupported_amounts_and_truncation(self):
        for amount in [0, -1, True, 1.1, "1000", 2**31]:
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                create_payment(amount_toman=amount)
        with self.assertRaises(ValueError):
            create_payment(amount_toman=1000, order_reference="a" * 101)

    def test_creation_timeout_does_not_create_a_second_remote_instruction(self):
        payment = create_payment(amount_toman=1000)
        client = provider_client()
        client.session.post.side_effect = requests.Timeout()
        with self.assertRaises(PayPingNetworkError):
            initiate_payment(
                payment.pk, return_url="https://merchant.example/", client=client
            )
        payment.refresh_from_db()
        self.assertEqual(payment.status, "uncertain")
        with self.assertRaises(PaymentCreationUncertain):
            initiate_payment(
                payment.pk, return_url="https://merchant.example/", client=client
            )
        self.assertEqual(client.session.post.call_count, 1)

    def test_stale_creation_is_not_automatically_reissued(self):
        payment = create_payment(amount_toman=1000)
        PayPingPayment.objects.filter(pk=payment.pk).update(
            status="creating", updated_at=timezone.now() - timedelta(hours=1)
        )
        client = provider_client()
        with self.assertRaises(PaymentCreationUncertain):
            initiate_payment(
                payment.pk, return_url="https://merchant.example/", client=client
            )
        client.session.post.assert_not_called()

    def test_creation_validation_failure_can_retry(self):
        payment = create_payment(amount_toman=1000)
        client = provider_client()
        response(client, 400, {"metaData": {"code": 105}})
        with self.assertRaises(PayPingAPIError):
            initiate_payment(payment.pk, return_url="bad", client=client)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "new")
        response(client, 200, {"paymentCode": str(uuid4())})
        self.assertIn(
            "/pay/start/",
            initiate_payment(
                payment.pk, return_url="https://merchant.example/", client=client
            ),
        )

    def test_factory_redirect_and_flags_use_same_configuration(self):
        payment = create_payment(
            amount_toman=1000,
            account="merchant-two",
            is_reversible=True,
            is_blocked=True,
        )
        client = provider_client(base_url="https://api.payping.io/v3")
        response(client, 200, {"paymentCode": str(uuid4())})
        factory = Mock(return_value=client)
        with override_settings(PAYPING_CLIENT_FACTORY=factory):
            url = initiate_payment(payment.pk, return_url="https://merchant.example/")
            self.assertTrue(url.startswith("https://api.payping.io/"))
            payload = client.session.post.call_args.kwargs["json"]
            self.assertTrue(payload["isReversible"])
            self.assertTrue(payload["IsBlocked"])
            payment.refresh_from_db()
            response(
                client,
                200,
                {
                    "code": payment.payment_code,
                    "amount": 1000,
                    "clientRefId": str(payment.client_ref_id),
                    "paymentRefId": 12345,
                },
            )
            result = self.callback(payment)
            self.assertEqual(result.status_code, 302)
            self.assertEqual(result.url, "/success/")
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(factory.call_args.args[0].account, "merchant-two")
        payment.refresh_from_db()
        self.assertTrue(payment.redirect_url.startswith("https://api.payping.io/"))

    def test_checkout_helper_builds_callback_and_reuses_attempt(self):
        client = provider_client()
        response(client, 200, {"paymentCode": str(uuid4())})
        request = RequestFactory().get("/", secure=True)
        with override_settings(PAYPING_CLIENT_FACTORY=lambda payment: client):
            payment = start_payment(
                request, amount_toman=1000, idempotency_key="shop:42:1"
            )
            again = start_payment(
                request, amount_toman=1000, idempotency_key="shop:42:1"
            )
        self.assertEqual(payment.pk, again.pk)
        self.assertEqual(client.session.post.call_count, 1)
        self.assertEqual(
            client.session.post.call_args.kwargs["json"]["ReturnUrl"],
            "https://testserver/payping/callback/",
        )

    def test_reconciliation_recovers_missing_callback(self):
        payment = self.pending()
        client = self.verification(payment)
        client.session.get.return_value.status_code = 200
        client.session.get.return_value.json.return_value = {
            "amount": 1000,
            "clientRefId": str(payment.client_ref_id),
            "paymentStatus": 5,
            "refId": "12345",
            "isPaid": False,
        }
        self.assertEqual(
            reconcile_payment(payment.pk, client=client).status, "succeeded"
        )
        self.assertIn("/v1/report/", client.session.get.call_args.args[0])

    def test_reconciliation_rejects_unrelated_report(self):
        payment = self.pending()
        client = self.verification(payment)
        client.session.get.return_value.status_code = 200
        client.session.get.return_value.json.return_value = {
            "amount": 1000,
            "clientRefId": str(uuid4()),
            "paymentStatus": 5,
            "refId": "12345",
        }
        with self.assertRaises(PaymentCallbackError):
            reconcile_payment(payment.pk, client=client)
        client.session.post.assert_not_called()

    def test_callback_processing_has_separate_result(self):
        payment = self.pending()
        client = self.verification(payment)
        response(client, 202, {})
        with override_settings(PAYPING_CLIENT_FACTORY=lambda payment: client):
            result = self.callback(payment)
        self.assertEqual(result.status_code, 202)
        self.assertIn(b"Do not pay again", result.content)

    def test_custom_result_handler_receives_payment_context(self):
        from django.http import HttpResponse

        payment = self.pending(order_reference="shop:42")
        handler = Mock(return_value=HttpResponse("custom result"))
        with override_settings(
            PAYPING_CLIENT_FACTORY=lambda payment: self.verification(payment),
            PAYPING_RESULT_HANDLER=handler,
        ):
            result = self.callback(payment, json_body=True)
        self.assertEqual(result.content, b"custom result")
        self.assertEqual(handler.call_args.kwargs["payment"].order_reference, "shop:42")
        self.assertEqual(handler.call_args.kwargs["outcome"], "success")

    def test_json_numeric_zero_is_cancellation(self):
        payment = self.pending()
        result = self.callback(payment, status=0, json_body=True)
        self.assertEqual(result.url, "/failure/")
        payment.refresh_from_db()
        self.assertEqual(payment.status, "canceled")

    def test_float_callback_amount_and_invalid_payload_are_rejected(self):
        payment = self.pending()
        result = self.callback(payment, json_body=True, amount=1000.9)
        self.assertEqual(result.status_code, 400)
        result = self.client.post("/payping/callback/", {"status": 1, "data": "[]"})
        self.assertEqual(result.status_code, 400)

    def test_default_redirect_disallows_external_hosts(self):
        payment = self.pending()
        with override_settings(PAYPING_FAILURE_URL="https://evil.example/"):
            result = self.callback(payment, status=0)
        self.assertEqual(result.status_code, 400)
        with override_settings(
            PAYPING_FAILURE_URL="https://trusted.example/",
            PAYPING_ALLOWED_REDIRECT_HOSTS=["trusted.example"],
        ):
            result = self.callback(payment, status=0)
        self.assertEqual(result.url, "https://trusted.example/")

    def test_callbacks_allow_only_supported_http_methods(self):
        self.assertEqual(self.client.put("/payping/callback/").status_code, 405)

    def test_support_recovery_requires_matching_report(self):
        payment = create_payment(amount_toman=1000)
        PayPingPayment.objects.filter(pk=payment.pk).update(status="uncertain")
        client = provider_client(base_url="https://api.payping.io/v3")
        client.session.get.return_value.status_code = 200
        client.session.get.return_value.json.return_value = {
            "amount": 1000,
            "clientRefId": str(uuid4()),
        }
        code = str(uuid4())
        with self.assertRaises(PaymentCallbackError):
            recover_payment_code(payment.pk, payment_code=code, client=client)
        client.session.get.return_value.json.return_value["clientRefId"] = str(
            payment.client_ref_id
        )
        result = recover_payment_code(payment.pk, payment_code=code, client=client)
        self.assertEqual(result.status, "pending")
        self.assertEqual(result.payment_code, code)
        self.assertTrue(result.redirect_url.startswith("https://api.payping.io/"))

    def test_invalid_creation_response_does_not_allow_blind_retry(self):
        payment = create_payment(amount_toman=1000)
        client = provider_client()
        response(client, 200, {})
        from django_payping.exceptions import PayPingResponseError

        with self.assertRaises(PayPingResponseError):
            initiate_payment(
                payment.pk, return_url="https://merchant.example/", client=client
            )
        payment.refresh_from_db()
        self.assertEqual(payment.status, "uncertain")

    def test_recovery_batch_rotates_past_old_unpaid_records(self):
        first = self.pending()
        second = self.pending()
        client = provider_client()
        client.session.get.return_value.status_code = 200

        def report(*args, **kwargs):
            code = client.session.get.call_args.args[0].rsplit("/", 1)[1]
            payment = PayPingPayment.objects.get(payment_code=code)
            return {
                "amount": 1000,
                "clientRefId": str(payment.client_ref_id),
                "isPaid": False,
                "paymentStatus": 1,
            }

        client.session.get.return_value.json.side_effect = report
        with override_settings(PAYPING_CLIENT_FACTORY=lambda payment: client):
            for _ in range(2):
                call_command(
                    "payping_recover", limit=1, stdout=StringIO(), stderr=StringIO()
                )
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNotNone(first.last_reconciled_at)
        self.assertIsNotNone(second.last_reconciled_at)

    def test_migration_preserves_existing_payment_records(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        old = [("django_payping", "0001_initial")]
        new = [("django_payping", "0002_payment_recovery_and_events")]
        executor = MigrationExecutor(connection)
        try:
            executor.migrate(old)
            legacy_model = executor.loader.project_state(old).apps.get_model(
                "django_payping", "PayPingPayment"
            )
            payment = legacy_model.objects.create(
                amount_toman=1500,
                order_reference="legacy:42",
                status="succeeded",
                payment_code=str(uuid4()),
                payment_ref_id="456",
                metadata={"legacy": True},
            )
            MigrationExecutor(connection).migrate(new)
            upgraded = PayPingPayment.objects.get(pk=payment.pk)
            self.assertEqual(upgraded.amount_toman, 1500)
            self.assertEqual(upgraded.payment_ref_id, "456")
            self.assertEqual(upgraded.status, "succeeded")
            self.assertEqual(upgraded.metadata, {"legacy": True})
            self.assertIsNone(upgraded.idempotency_key)
            self.assertFalse(upgraded.events.exists())
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
