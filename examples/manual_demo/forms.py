from django import forms

from django_payping.constants import MAX_AMOUNT_TOMAN


class CheckoutForm(forms.Form):
    amount_toman = forms.IntegerField(
        label="Amount (toman)", min_value=1, max_value=MAX_AMOUNT_TOMAN, initial=1000
    )
