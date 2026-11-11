# Contributing

Work in short branches, open a pull request, and wait for one review before merging into `main`.

1. Branch from `main`: `git switch -c <your-name>/<short-task>`.
2. Keep each pull request to one task with a test that shows it is done.
3. Run `python -m pytest -q` before pushing. CI runs the same suite on Linux and Windows, Python 3.11 and 3.12.
4. Never commit keys or secrets. Use environment variables. `.env*` is ignored.
5. One authority decides refunds: `app/authority.py`. Do not add another path to the PayPal adapter.
6. Sandbox only. Live PayPal credentials are never used.
7. Commit under your own name so the contribution record is accurate.

