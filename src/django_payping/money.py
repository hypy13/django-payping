from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

TOMAN_CURRENCIES = {
    "irt",
    "toman",
    "iran toman",
    "iranian toman",
    "iran-toman",
    "iranian-toman",
    "iran_toman",
    "iranian_toman",
    "تومان",
    "تومان ایران",
}


def amount_to_toman(amount: int | float | Decimal | str, currency: str) -> int:
    """Convert supported Iranian currencies, rounding half up to whole toman.

    Raises:
        ValueError: Unsupported currency, invalid amount, or a result below one toman.
    """
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("مبلغ قابل تبدیل نیست.") from exc

    if not value.is_finite() or value <= 0:
        raise ValueError("مبلغ باید عددی مثبت و معتبر باشد.")

    if not isinstance(currency, str):
        raise ValueError("currency must be a supported currency code.")
    normalized_currency = currency.strip().lower()
    if normalized_currency in TOMAN_CURRENCIES:
        multiplier = Decimal(1)
    elif normalized_currency == "irht":
        multiplier = Decimal(1000)
    elif normalized_currency == "irhr":
        multiplier = Decimal(100)
    elif normalized_currency == "irr":
        multiplier = Decimal("0.1")
    else:
        raise ValueError(f"واحد پول پشتیبانی نمی‌شود: {currency}")

    try:
        result = int(
            (value * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
    except InvalidOperation as exc:
        raise ValueError("Amount exceeds the supported decimal precision.") from exc
    if result < 1:
        raise ValueError("Converted amount must be at least one toman.")
    return result
