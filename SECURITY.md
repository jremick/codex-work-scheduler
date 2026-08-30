# Security policy

## Supported versions

During public alpha, security fixes target the latest commit on the default branch and the latest alpha prerelease. Older commits and local modifications are not supported.

## Report a vulnerability

Do not open a public issue for a suspected vulnerability.

Use GitHub's private vulnerability reporting flow from the repository **Security** tab. If the **Report a vulnerability** action is unavailable, do not post sensitive details or open a public issue.

Include only the minimum information needed to reproduce the issue:

- Affected commit or release.
- A concise description of the impact.
- Reproduction steps that use synthetic data.
- The affected command, state transition, or trust boundary.
- A suggested mitigation, if known.

Do not include authentication tokens, account email addresses, account fingerprints, exact quota usage, paid-credit details, private prompts, task output, local database contents, or machine-specific configuration. Replace those values with clearly marked placeholders.

## Scope

Useful reports include approval bypasses, unsafe dispatch, workspace escape, unintended network access, secret or prompt disclosure, audit-chain bypass, lease or concurrency failures, unsafe retry, and state corruption that can cause an unauthorized run.

Account billing disputes, Codex service availability, model behavior, and vulnerabilities in Codex itself are outside this repository's control. Report upstream product issues through the appropriate OpenAI channel.

No response or remediation time is guaranteed during public alpha. The maintainer may pause public use while a serious issue is assessed.
