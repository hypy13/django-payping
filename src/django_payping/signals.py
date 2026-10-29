from django.dispatch import Signal

payment_succeeded = Signal()
payment_failed = Signal()
payment_canceled = Signal()
payment_created = Signal()
payment_initiated = Signal()
payment_creation_failed = Signal()
payment_processing = Signal()
payment_duplicate = Signal()
