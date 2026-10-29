class PayPingError(Exception):
    """Base exception for PayPing integration errors."""


class PayPingConfigurationError(PayPingError):
    """Raised when the gateway is missing required configuration."""


class PayPingNetworkError(PayPingError):
    """Raised when PayPing cannot be reached."""


class PayPingResponseError(PayPingError):
    """Raised when PayPing returns an unexpected response."""


class PayPingAPIError(PayPingError):
    """Raised for an unsuccessful response from the PayPing API."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        error_code: int | None = None,
        trace_id: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.trace_id = trace_id


class PaymentCallbackError(PayPingError):
    """Raised when callback data does not match the stored payment."""


class PaymentAlreadyProcessing(PayPingError):
    """Raised when another request is currently creating this payment."""


class PaymentVerificationPending(PayPingError):
    """Verification is unfinished and must be retried, without charging again."""


class PaymentCreationUncertain(PayPingError):
    """Creation may have reached PayPing; automatic recreation is unsafe."""
