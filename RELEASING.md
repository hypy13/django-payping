# Releasing django-payping

1. Install the development dependencies with `python -m pip install -e '.[dev]'`.
2. Run the tests, migration check, mypy, and Ruff commands in `CONTRIBUTING.md`.
3. Update `CHANGELOG.md` and keep the version in `pyproject.toml` and
   `src/django_payping/__init__.py` identical.
4. Build with `python -m build` and validate with
   `python -m twine check --strict dist/*`. Inspect the archives for migrations,
   `py.typed`, and compiled translation catalogs. Private `resources/` material
   must stay outside distributions.
5. Install the wheel in a clean environment and run the tests against that
   installed package. Check each supported Django/Python combination in CI.
6. Create a GitHub release whose tag is `v` followed by the package version.
   The publish workflow validates the version, runs CI, and publishes the checked
   distributions using the repository's configured PyPI trusted publisher.

Configure the GitHub `pypi` environment and the corresponding PyPI trusted publisher
before publishing the first release. Do not put API tokens in the package or Git.
