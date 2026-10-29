# django-payping

A reusable Django application for PayPing checkout payments.
The host project owns orders, customer authorization, pricing, and fulfillment.
This package owns provider communication, payment records, callbacks, recovery,
and persistent payment events.

## Install

For a step-by-step integration with an existing Django project, see the
[installation guide](INSTALLATION.md).

```sh
pip install "django-payping @ git+https://github.com/hypy13/django-payping.git"
```

This command works after the source is pushed to GitHub and requires Git.
For reproducible installations, append a release tag such as `@v0.1.0` to the
repository URL once that tag is published. After the first PyPI release, install
with `python -m pip install django-payping`.

Requires Python 3.10+ and Django 5.2, 6.0, or 6.1. Django 6.x requires
Python 3.12+. See [CONTRIBUTING.md](CONTRIBUTING.md) for local development and
[RELEASING.md](RELEASING.md) for publication instructions.

```python
# settings.py
import os

INSTALLED_APPS = [
    # ...
    "django_payping.apps.DjangoPayPingConfig",
]
PAYPING_TOKEN = os.environ["PAYPING_TOKEN"]
PAYPING_BASE_URL = "https://api.payping.ir/v3"  # .io is also supported
PAYPING_TIMEOUT = 45
PAYPING_SUCCESS_URL = "orders:payment_success"
PAYPING_FAILURE_URL = "orders:payment_failed"
```

```python
# Project urls.py
from django.urls import include, path

urlpatterns = [
    path("payments/payping/", include("django_payping.urls")),
]
```

```sh
python manage.py migrate
```

Existing installations must also run migrations. Migration 0002 adds fields and an
event table without replacing existing payment records. Migration 0003 adds
translatable labels, event choices, and model validators while preserving existing
field names, tables, payment records, and event delivery state. Historical payments do not
receive synthetic fulfillment events. Previously lost host updates should be
reconciled by the host using its payment records.

## Typed service API

Use `PaymentService` when you want structured inputs and an injectable gateway.
Its payment operations return `PayPingPayment`; `initiate_payment()` returns a
redirect URL. Your IDE can discover all request fields, provider response keys,
and service methods:

```python
from django_payping.services import PaymentService
from django_payping.types import PaymentRequest

service = PaymentService()
payment = service.create(
    PaymentRequest(
        amount_toman=1000,
        order_reference="shop.Order:42",
        idempotency_key="shop.Order:42:attempt:1",
        metadata={"order_id": "42"},
    )
)
redirect_url = service.initiate_payment(
    payment.pk,
    return_url="https://example.com/payments/payping/callback/",
)
```

The existing function API remains supported and uses the same service.
`start_payment()` exposes explicit typed keyword arguments for host checkout views.
Use `PaymentCallback` with `service.verify_callback()` for custom callback entry
points; use `service.reconcile_payment()` for recovery. Inputs remain untrusted
until validated by the service. A `PaymentRequest` is a frozen dataclass, but its
metadata may contain mutable JSON values; the service copies and normalizes
metadata before persisting it or comparing an idempotency key. Unsupported or
non-finite JSON values raise `ValueError` before a payment is created.

Pass `PaymentService(client=your_gateway)` to reuse a client. Custom clients can
implement the `PaymentGateway` protocol without inheriting from `PayPingClient`.
`CreatePaymentPayload`, `CreatePaymentResponse`, `VerifyPaymentResponse`, and
`PaymentReport` describe provider wire data; responses retain additional provider
metadata. Protocols named `ClientFactory`, `EventHandler`, `ResultHandler`, and
`ResultUrlResolver` describe the supported host hooks. Import these types from
`django_payping.types`.

ORM field names retain their existing meaning and lookup compatibility:

| Field | Meaning |
| --- | --- |
| `id` | Local payment record UUID |
| `client_ref_id` | Local attempt UUID sent to PayPing as `ClientRefId` |
| `payment_code` | Provider instruction code used for redirect and verification |
| `payment_ref_id` | Provider transaction reference after a payment |
| `order_reference` | Host application's namespaced order identifier |
| `account` | Non-secret merchant routing key |
| `amount_toman` | Integer toman amount, excluding provider fees |

All model fields, model names, status labels, and event kinds use lazy translation.
Persian translations are bundled. `PayPingPayment.Status` and `PayPingEvent.Kind`
provide named choices. Amount validators also apply to model forms and
`full_clean()`; Django's ordinary `save()` and queryset writes do not run model
validation. Use the service to enforce payment input rules.

## Start a payment

Call from an authorized host view after loading the order and calculating its
price on the server:

```python
from django.shortcuts import redirect
from django_payping.checkout import start_payment

payment = start_payment(
    request,
    amount_toman=order.total_toman,
    order_reference=f"shop.Order:{order.pk}",
    idempotency_key=f"shop.Order:{order.pk}:attempt:{order.payment_attempt_id}",
    payer_name=order.customer_name,
    payer_identity=order.customer_mobile,
    description=f"Order {order.pk}",
    metadata={"flow": "shop", "order_id": str(order.pk)},
)
return redirect(payment.redirect_url)
```

Amounts are positive integer **toman**, up to the API's int32 limit. To convert
IRR, IRT, IRHR, or IRHT, use `django_payping.money.amount_to_toman()`.
Conversion rounds half up to whole toman and raises `ValueError` when the result
would be zero or the input is invalid.

A business idempotency key identifies one deliberate attempt and must remain
stable across repeat submissions. Reusing it returns the same local payment;
changing its amount, reference, payer, account, metadata, or flags raises ValueError.
Namespace keys and references when several Django apps use the gateway.
Omitting the key deliberately creates a new independent attempt.

The amount is a snapshot. After an order changes, the host must resolve its active
attempt before starting a distinct attempt with a new key. An idempotency key
prevents duplicate instructions for the same local attempt; it does not enforce
one payment across arbitrary new keys for the same order.

For background jobs or custom checkout views, the lower-level API remains:

```python
from django_payping.services import create_payment, initiate_payment

payment = create_payment(amount_toman=1000, order_reference="shop:42")
gateway_url = initiate_payment(payment.pk, return_url="https://example.com/payments/payping/callback/")
```

`create_payment()` also accepts `account`, `is_reversible`, and `is_blocked`.
Creation and verification responses preserve provider fee, VAT, and final-amount
metadata; confirmed card information is stored in `card_number` and `card_hash_pan`.

## Use Django REST Framework generic views

The payment services can be called from DRF class-based views. DRF is optional;
install and configure `djangorestframework` in the host project when using it.
The example below uses the standard `GenericAPIView` API and assumes the host
has an Order model. Adapt its field names and URL converter to your project.

```python
# orders/api.py
from rest_framework import generics, serializers
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from django_payping.checkout import start_payment
from .models import Order


class PaymentRedirectSerializer(serializers.Serializer):
    payment_id = serializers.UUIDField(source="pk", read_only=True)
    redirect_url = serializers.URLField(read_only=True)


class StartOrderPaymentView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PaymentRedirectSerializer

    def get_queryset(self):
        # Restrict lookup to orders belonging to the authenticated customer.
        return Order.objects.filter(customer=self.request.user)

    def post(self, request, *args, **kwargs):
        order = self.get_object()
        if order.is_paid:
            raise ValidationError("This order is already paid.")

        payment = start_payment(
            request,
            amount_toman=order.total_toman,
            order_reference=f"shop.Order:{order.pk}",
            idempotency_key=f"shop.Order:{order.pk}:attempt:{order.payment_attempt_id}",
            metadata={"flow": "shop", "order_id": str(order.pk)},
        )
        return Response(self.get_serializer(payment).data)
```

```python
# Project urls.py — retain the PayPing callback include shown above.
from django.urls import path
from orders.api import StartOrderPaymentView

urlpatterns += [
    path("api/orders/<int:pk>/pay/", StartOrderPaymentView.as_view(), name="order-pay"),
]
```

An authenticated `POST /api/orders/42/pay/` needs no request body. Price, order
reference, and the persisted attempt identifier come from the host database.
Keep `payment_attempt_id` stable across repeated submissions; generating it in
every POST would bypass business idempotency. The host must also enforce its
order eligibility and active-attempt rules described above.

The successful JSON response contains `payment_id` and `redirect_url`. The
frontend navigates the browser to `redirect_url`, for example with
`window.location.assign(result.redirect_url)`. Fetching this API endpoint does
not itself navigate the browser to the bank. Apply your host's normal DRF
authentication and CSRF rules to this checkout endpoint.

Translate package exceptions in the view or your project's DRF exception handler;
DRF does not translate them automatically. The following is an example HTTP
mapping for the host, rather than built-in behavior:

| Exception | Suggested API response and next action |
| --- | --- |
| `PaymentVerificationPending` | HTTP 202; wait for reconciliation and do not initiate another charge |
| `PaymentAlreadyProcessing` | HTTP 409; creation is in progress, retry with the same attempt key |
| `PaymentCreationUncertain` | HTTP 409; resolve the existing instruction before allowing a new attempt |
| `PaymentCallbackError` or idempotency-conflict `ValueError` | HTTP 409; inspect the stored payment and attempt state |
| Other `PayPingError` | HTTP 502; return a generic provider error and inspect the stored state before retrying |

The same services work inside `APIView` methods or ViewSet actions. Here,
`GenericAPIView.get_queryset()` scopes the order lookup,
`GenericAPIView.get_object()` preserves object permission checks, and
`GenericAPIView.get_serializer()` formats the response. `post()` orchestrates
the payment service because this endpoint starts a payment for an existing order.
See the [DRF generic-view reference](https://www.django-rest-framework.org/api-guide/generic-views/)
and [Classy DRF's GenericAPIView method reference](https://www.cdrf.co/3.16/rest_framework.generics/GenericAPIView.html).
The latter is a DRF 3.16 reference; this example does not pin the host's DRF version.

The bundled PayPing callback remains a Django `View` and can be used unchanged
in a DRF project: PayPing sends its form POST directly to that URL. Configure
`PAYPING_RESULT_HANDLER` or subclass `PayPingCallbackView` to customize its response,
as described below. Fulfill orders through verified payment events, not the
checkout API response. This package does not provide DRF-specific generic views,
serializers, or exception handlers; the classes above belong to the host project.

## Fulfill orders reliably

Configure a handler for persistent events:

```python
PAYPING_EVENT_HANDLER = "orders.payments.handle_payment_event"
```

```python
# orders/payments.py — adapt field and method names to your order model
from django.db import transaction
from .models import Order


def handle_payment_event(event):
    if event.kind != "succeeded":
        return
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=event.payment.metadata["order_id"])
        if order.is_paid:
            return
        order.mark_paid(transaction_id=event.payment.payment_ref_id)
```

Payment state and its event are written in the same transaction. Delivery runs
after commit. A receiver/handler failure is logged and stored on the event;
a successful payment remains successful and the event remains undelivered.
Run the recovery command to retry it. Repeated successful callbacks also retry
undelivered events for that payment.

Delivery is **at least once**. Handlers must be idempotent: retries, partial success,
worker crashes, or expired leases can repeat a handler. For external side effects,
use the stable `event.id` as the receiving system's idempotency key. A handler may
observe a newer payment state than the event; `event.kind` and `event.payload`
identify the original transition.

The existing signals remain supported and now receive both `payment` and `event`:

- `payment_created`, `payment_initiated`, `payment_creation_failed`
- `payment_processing`, `payment_succeeded`, `payment_failed`, `payment_canceled`
- `payment_duplicate` (one notification per local payment)

Register receivers in the host AppConfig.ready(). If one receiver fails, delivery
of the event is retried, including receivers that already succeeded. Keep all
receivers idempotent. Do not perform the same fulfillment in both a signal and the
configured event handler.

The read-only Django admin includes the payment's event timeline, delivery
attempts, and last errors.

## Customize callbacks and result pages

PayPing sends a form POST; GET and JSON callbacks are also accepted. The view
validates the stored code, reference, and amount before provider verification.
Only HTTP 200 completes verification; documented 409/code 110 is accepted as an
already verified transaction. HTTP 202/502 remain processing. Browser cancellation
is advisory: it cannot interrupt verification or downgrade success, and a later
provider-confirmed success can recover a canceled/failed record.

For multiple host apps, use a payment-aware result handler:

```python
PAYPING_RESULT_HANDLER = "orders.payments.payment_result"
```

```python
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect


def payment_result(*, request, payment, outcome):
    if outcome == "invalid":
        return HttpResponseBadRequest("Invalid callback")
    if outcome == "processing":
        return HttpResponse("Verification is processing; do not pay again.", status=202)
    # Route by payment.metadata['flow'] when different apps share the gateway.
    return redirect("orders:payment_result", payment_id=payment.pk)
```

Possible outcomes are `success`, `failure`, `canceled`, `processing`, and `invalid`.
An invalid callback passes `payment=None`. Result pages must authorize access to
the payment and read database state, rather than trust query-string status.
Callback processing must work without a logged-in session.

Without a result handler, the default view uses PAYPING_SUCCESS_URL,
PAYPING_FAILURE_URL, and optional PAYPING_PROCESSING_URL. A processing result
returns HTTP 202 when no processing URL is configured. Each URL setting can be a
route name, local URL, or callable accepting `(request, payment, outcome)` as keyword
arguments and returning a URL. External redirect hosts require an explicit
`PAYPING_ALLOWED_REDIRECT_HOSTS` list. Custom result handlers own their redirects.

For deeper changes, subclass `django_payping.views.PayPingCallbackView` and override
`parse_callback()` or `render_result()`; set `PAYPING_CALLBACK_VIEW` to its import
path, or mount the subclass directly in host URLs. The old
`payment_callback_view` import remains available.

## Configure clients and merchant accounts

For a single merchant, the default client uses global token/timeout settings and
the payment's stored base URL. The .ir/.io base URL used at creation is persisted,
so later redirect construction does not change when global settings change.

For different credentials, subclasses, sessions, or merchant accounts:

```python
PAYPING_CLIENT_FACTORY = "billing.payping_clients.client_for_payment"
```

```python
from django_payping.client import PayPingClient


def client_for_payment(payment):
    account = lookup_merchant_configuration(payment.account)
    return PayPingClient(
        token=account.token,
        base_url=payment.gateway_base_url or account.base_url,
        timeout=45,
    )
```

The factory receives the persisted payment during creation, callbacks, and
reconciliation. The `account` field is a non-secret routing key; keep credentials
in server configuration. Explicit `client=` injection is still available in the
lower-level services, but production callbacks must be able to obtain that same
account configuration through the factory.

## Recover unfinished payments and events

Schedule this command approximately every minute using your project's scheduler:

```sh
python manage.py payping_recover --limit 100
```

It checks pending/processing/canceled/failed records that have a provider code,
then retries undelivered events. Options: `--events-only`, `--payment UUID`, and
`--limit N`. The token needs access to the documented v1 report endpoint
(`pay:read`) when recovering a missing callback. The normal callback verification
flow does not require a report lookup.

The supplied PayPing documentation requires verification within **ten minutes
of payment**. Schedule recovery promptly, monitor failures, and increase the batch
size or run targeted jobs when volume requires it. The command visits records checked least recently first, so an old unpaid
instruction does not permanently block newer payments in a limited batch. Your host may also call:

```python
from django_payping.services import reconcile_payment
from django_payping.events import deliver_pending_events

payment = reconcile_payment(payment_id)
deliver_pending_events(limit=100)
```

After a creation timeout, invalid success response, server error, or stale worker,
the record becomes `uncertain`: the provider might already have created an
instruction. The app does not automatically issue another one. A known HTTP 4xx
creation rejection leaves it `new`, allowing correction and retry.

If no provider code was received, the report endpoint cannot locate it. Resolve
that attempt with PayPing support using its client_ref_id and trace information.
If support provides the code, validate and attach it using:

```python
from django_payping.services import recover_payment_code, reconcile_payment

recover_payment_code(payment_id, payment_code=code_from_support)
reconcile_payment(payment_id)
```

Neither a processing payment nor an uncertain creation should be offered as a
fresh bank redirect. The host must distinguish those exceptions from a resolved
failed attempt before allowing another charge.

Optional event settings:

```python
PAYPING_AUTO_DELIVER_EVENTS = True  # False: workers deliver events exclusively
PAYPING_EVENT_LEASE_SECONDS = 300   # Must exceed the normal handler duration
```

## Scope and development

### Try the payment flow in your browser

The standalone [manual demo](examples/manual_demo/) provides an HTML checkout
that creates a payment through the real PayPing API, redirects to PayPing, and
verifies the callback. The result page shows the saved status and events.
A live merchant token can charge real money. Use this demo for development only;
it has no authentication and is not a production shop.

From the repository root, after installing the package and Django:

```sh
export PAYPING_TOKEN='your-payping-token'
# Set a callback URL reachable through your development host/tunnel:
export PAYPING_RETURN_URL='https://your-development-host.example/payping/callback/'
PYTHONPATH=src:. python -m django migrate --settings=examples.manual_demo.settings
PYTHONPATH=src:. python -m django runserver 127.0.0.1:8000 --settings=examples.manual_demo.settings
```

Open <http://127.0.0.1:8000/>, enter an amount in toman, and click
**Continue to PayPing**. Complete or cancel the payment on the actual gateway.
The callback returns to the result page. Payments are saved in the demo's ignored
SQLite database. Keep the token in your environment, never in the HTML or Git.
If `PAYPING_RETURN_URL` is omitted, the callback URL is built from the browser
request host; use a return URL accepted by your PayPing merchant configuration.
For a test token, use an amount permitted by that token.

If verification remains `processing`, recover that payment instead of paying again:

```sh
PYTHONPATH=src:. python -m django payping_recover --settings=examples.manual_demo.settings
```

Implemented: ordinary checkout, conversion, callbacks, identity validation,
verification/recovery, configurable host integration, event history, payer/card
metadata, and reversible/blocked creation flags. This remains a checkout adapter;
refund execution, split settlement, unblock/delete operations, invoices, BNPL,
and the other platform APIs are not implemented. Enabling a creation flag does
not itself perform a refund or release blocked funds.

Run tests and migration checks:

```sh
PYTHONPATH=src:. python -m django test tests --settings=tests.settings
PYTHONPATH=src:. python -m django makemigrations --check --dry-run --settings=tests.settings
PYTHONPATH=src:. python -m mypy
python -m ruff check src tests
python -m ruff format --check src tests
```

Tests mock HTTP and use SQLite; no live charges are made. Production-database
concurrency and real merchant sandbox integration require separate validation.
