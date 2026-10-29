# Install and use django-payping

This guide installs django-payping from GitHub and connects it to an existing
Django project. Your project owns orders, customer authorization, pricing, and
fulfillment. The package handles PayPing requests, payment records, callbacks,
verification, and recovery.

## 1. Install the package

Activate your Django project's virtual environment, then run:

```sh
python -m pip install "django-payping @ git+https://github.com/hypy13/django-payping.git"
```

Git must be installed. The package requires Python 3.10+ and Django 5.2 or later
(below Django 7). Django 6.x requires Python 3.12+.

If your project uses `requirements.txt`, add:

```text
django-payping @ git+https://github.com/hypy13/django-payping.git
```

Then install your requirements normally:

```sh
python -m pip install -r requirements.txt
```

For reproducible production installations, pin a published tag or full commit SHA
by appending `@TAG_OR_COMMIT` to the repository URL. Replace that placeholder with
an actual revision before installing.

## 2. Configure Django

Add the app and payment settings to your project's `settings.py`:

```python
import os

INSTALLED_APPS = [
    # Your existing apps...
    "django_payping.apps.DjangoPayPingConfig",
]

PAYPING_TOKEN = os.environ["PAYPING_TOKEN"]
PAYPING_BASE_URL = "https://api.payping.ir/v3"
PAYPING_TIMEOUT = 45

# Implement these result pages in your project.
PAYPING_SUCCESS_URL = "/orders/payment-success/"
PAYPING_FAILURE_URL = "/orders/payment-failed/"

# Create this handler in step 5.
PAYPING_EVENT_HANDLER = "orders.payments.handle_payment_event"
```

Set your merchant token in the environment before running Django commands:

```sh
export PAYPING_TOKEN="your-payping-token"
```

Configure the same variable in your deployment environment. Keep the token out of
source control and frontend code.

## 3. Add the callback URL and migrate

Add the package URLs to your project's `urls.py`:

```python
from django.urls import include, path

urlpatterns = [
    # Your existing routes...
    path("payments/payping/", include("django_payping.urls")),
]
```

Create the payment and event tables:

```sh
python manage.py migrate
```

With this configuration, your callback address is:

```text
https://your-domain.com/payments/payping/callback/
```

Use a public address accepted by your PayPing merchant configuration. For local
development, use an appropriate public development host or tunnel.

The checkout helper builds the callback URL from the incoming request. Ensure
Django sees the correct public domain and HTTPS scheme when deployed behind a
proxy. You can also pass an explicit absolute `return_url` to `start_payment()`.
The bundled callback handles verification and does not require a logged-in session.

## 4. Start a payment for an order

The following example assumes an `orders` app with an `Order` model containing:

| Field | Purpose |
| --- | --- |
| `customer` | User who owns the order |
| `total_toman` | Server-calculated price as a positive integer in toman |
| `is_paid` | Whether the order has already been paid |
| `payment_attempt_id` | Persisted identifier for the current payment attempt |

These fields belong to your project; the package does not create an Order model.
Adapt the names and URL converter to your model.

```python
# orders/views.py
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from django_payping.checkout import start_payment

from .models import Order


@login_required
@require_POST
def pay_order(request, pk):
    order = get_object_or_404(Order, pk=pk, customer=request.user)

    if order.is_paid:
        return HttpResponseBadRequest("This order is already paid.")

    payment = start_payment(
        request,
        amount_toman=order.total_toman,
        order_reference=f"orders.Order:{order.pk}",
        idempotency_key=(
            f"orders.Order:{order.pk}:attempt:{order.payment_attempt_id}"
        ),
        description=f"Order {order.pk}",
        metadata={"order_id": str(order.pk)},
    )
    return redirect(payment.redirect_url)
```

Add the checkout route to your project's `urls.py`, retaining the callback include:

```python
from orders.views import pay_order

urlpatterns += [
    path("orders/<int:pk>/pay/", pay_order, name="order-pay"),
]
```

Submit checkout with a POST form:

```html
<form method="post" action="{% url 'order-pay' order.pk %}">
  {% csrf_token %}
  <button type="submit">Pay with PayPing</button>
</form>
```

Amounts are **toman**, not rial. Calculate prices on the server rather than accepting
them from the browser.

Keep `payment_attempt_id` stable across repeat submissions. Do not generate a new
identifier on every POST. Reusing the same idempotency key returns the same local
payment; changing its amount or other creation inputs raises `ValueError`.

Your checkout must also enforce order eligibility and active-attempt rules. Before
changing an order's price or allowing a new attempt, resolve the existing attempt.
Different idempotency keys can create separate payments for the same order.

### Handle payment errors

Adapt exception handling to your project's error pages or API responses:

| Exception from `django_payping.exceptions` | Host action |
| --- | --- |
| `PaymentAlreadyProcessing` | Creation is in progress; retry with the same attempt key |
| `PaymentVerificationPending` | Wait for reconciliation before allowing another payment |
| `PaymentCreationUncertain` | Resolve the existing instruction before creating another attempt |
| Other `PayPingError` | Show a generic provider error and inspect stored payment state before retrying |

A processing payment or uncertain creation must not be treated as a fresh checkout
attempt. See the [README recovery guidance](README.md#recover-unfinished-payments-and-events)
for resolving these states.

## 5. Mark orders paid after verification

Create the event handler referenced by `PAYPING_EVENT_HANDLER`:

```python
# orders/payments.py
from django.db import transaction

from .models import Order


def handle_payment_event(event):
    if event.kind != "succeeded":
        return

    with transaction.atomic():
        order = Order.objects.select_for_update().get(
            pk=event.payment.metadata["order_id"],
        )
        if order.is_paid:
            return

        # Implement this method on your Order model to persist payment state.
        order.mark_paid(transaction_id=event.payment.payment_ref_id)
```

`mark_paid()` is a host-project method, not a package API. Implement it to save your
order's paid status and provider reference, or replace it with your existing order
fulfillment logic.

Events are delivered at least once. Keep the handler idempotent so retries cannot
repeat fulfillment. For external side effects, use `event.id` as the receiving
system's idempotency key.

Mark orders paid through verified events. Result pages should display database
state and authorize access to any order or payment details. A success redirect
alone is not proof of payment. The callback does not depend on the customer's
session being available.

If multiple apps share this gateway, route events by a namespaced
`order_reference` or a flow identifier in `metadata` before looking up orders.

## 6. Schedule payment and event recovery

Run this command approximately every minute using cron, a worker scheduler, or
your deployment platform's scheduled jobs:

```sh
python manage.py payping_recover --limit 100
```

Run it with your project's virtual environment, settings, and merchant credentials.
It reconciles unfinished payments with known provider codes and retries undelivered
events. Report-based recovery requires the token's `pay:read` permission; normal
callback verification does not require a report lookup.

Schedule recovery promptly: the provider documentation supplied with this project
requires verification within ten minutes of payment. Monitor recovery failures and
adjust batch size to your payment volume.

## Next steps

See [README.md](README.md) for the DRF example, custom result handlers, multiple
merchant accounts, payment status recovery, and the standalone manual demo.
