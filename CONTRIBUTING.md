# Development

Create an isolated environment and install the editable package:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m django test tests --settings=tests.settings
python -m django makemigrations --check --dry-run --settings=tests.settings
python -m mypy
python -m ruff check src tests
python -m ruff format --check src tests
python -m build
python -m twine check dist/*
```

Tests use SQLite and mocked HTTP. They do not make live payments. CI exercises
Django 5.2 on Python 3.10–3.14 and Django 6.0/6.1 on Python 3.12–3.14.
Validate production database concurrency and merchant integration separately.

Public interfaces must have useful input and return annotations. Use
`PaymentRequest` and `PaymentCallback` for service inputs and the `PaymentGateway`
protocol for injected clients. Keep provider wire names in typed response schemas;
keep application field names in Python's snake case. Extend the existing migration
history rather than rewriting published migrations.

Model labels use `gettext_lazy`. When updating translations, regenerate and compile
the Persian catalog with Django's `makemessages` and `compilemessages` commands
(GNU gettext is required). Commit both `.po` and `.mo` files so installed packages
can translate labels without a build-time gettext dependency.

Keep reference documents and upstream source under `resources/`, including
`resources/REVIEW.md`, `resources/docs-payping-ir.md`, and
`resources/woo-payping-gateway/`.
Git ignores that directory; its contents remain available locally but are not backed up by
GitHub. Package distributions also exclude them. Do not force-add reference
material or credentials. Public source, tests, and contributor documentation
belong in Git.

See [RELEASING.md](RELEASING.md) for the release procedure.
