# Contributing

Focused contributions are welcome. Unless you explicitly state otherwise, contributions submitted for inclusion in this repository are licensed under the [Apache License 2.0](LICENSE), without additional terms or a separate contributor license agreement.

## Before a pull request

Open an issue before a large behavior, schema, service, security-boundary, or dependency change. Small fixes can go directly to a pull request when their scope and safety impact are clear.

Keep changes narrow. Do not add new authentication paths, network listeners, external notifications, automatic retries, paid-credit use, or dependencies without an accepted design and explicit maintainer approval.

Never include credentials, account identity, exact quota usage, private prompts, task output, local scheduler state, approval files, logs, or machine-specific configuration. Use synthetic fixtures.

## Verification

Run the complete local check set:

```bash
python3 -m py_compile codex_work_scheduler/*.py
python3 -m unittest discover -s tests -v
python3 scripts/check_json.py
python3 scripts/check_local_links.py
python3 scripts/check_public_safety.py --history
git diff --check
```

Add tests for changed behavior and explain why the behavior matters. For public documentation, verify every local link and keep alpha limitations visible.

## Pull-request content

Describe:

- The problem and intended result.
- The affected safety or compatibility boundary.
- The tests and manual checks run.
- Any skipped verification or unresolved risk.
- Whether the change affects configuration, state, approvals, App Server methods, launchd operation, or public documentation.

Do not include secret-bearing logs or raw account responses. Maintainers may request a smaller change or decline work that widens the alpha support surface.
