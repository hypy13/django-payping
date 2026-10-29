# Changelog

## 0.1.0 — Unreleased

- Added an injectable `PaymentService`, typed request/callback dataclasses, provider
  response schemas, and host hook protocols; existing service functions remain supported.
- Added explicit checkout arguments, translated model labels and Persian catalogs,
  event choices, shared timestamps, and amount validators in migration 0003.
- Normalized JSON metadata before idempotency comparison, rejected conflicting
  transaction references, and rejected conversions that round to zero toman.
- Added regression tests, static type checks, and lint/format checks.

Initial release: reusable PayPing checkout integration with payment persistence,
callbacks, verification and recovery, durable payment events, Django admin,
currency conversion, and configurable host application hooks.
