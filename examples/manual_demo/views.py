from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from django_payping.checkout import start_payment
from django_payping.exceptions import PayPingError
from django_payping.models import PayPingPayment
from django_payping.types import PaymentOutcome
from examples.manual_demo.forms import CheckoutForm


@require_http_methods(["GET", "POST"])
def checkout_view(request: HttpRequest) -> HttpResponse:
    form = CheckoutForm(request.POST if request.method == "POST" else None)
    configured = bool(settings.PAYPING_TOKEN)
    if request.method == "POST" and configured and form.is_valid():
        try:
            payment = start_payment(
                request,
                amount_toman=form.cleaned_data["amount_toman"],
                order_reference="manual-demo",
                description="Manual PayPing gateway test",
                return_url=settings.PAYPING_RETURN_URL or None,
            )
        except PayPingError as exc:
            form.add_error(None, str(exc))
            form.add_error(
                None,
                "Inspect the saved payment before retrying: a timeout may mean "
                "PayPing already created the payment.",
            )
        else:
            return redirect(payment.redirect_url)
    return render(
        request,
        "payment.html",
        {"step": "checkout", "form": form, "configured": configured},
    )


def payment_result(
    *, request: HttpRequest, payment: PayPingPayment | None, outcome: PaymentOutcome
) -> HttpResponse:
    return render(
        request,
        "payment.html",
        {"step": "result", "payment": payment, "outcome": outcome},
        status=400 if outcome == "invalid" else 200,
    )
