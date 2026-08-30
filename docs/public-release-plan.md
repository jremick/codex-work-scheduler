# Public release plan

Status: **Stage 1 complete - sanitized private release candidate**

Target: **Stage 2 - Public Alpha with an alpha prerelease**

Last reviewed: **2026-08-30**

This plan records release evidence and the final approval boundary. It does not authorize a visibility change, tag, release, package publication, authentication change, or announcement.

## Current release candidate

- The repository explains its safety boundary, alpha limitations, source-checkout quick start, disabled background supervisor, support, security, contribution terms, compatibility, and Apache-2.0 license.
- The background supervisor and launchd staging surface are integrated. The example remains disabled, and the repository has no launchd installation or activation command.
- Runtime state, local configuration, SQLite files, logs, environment files, approvals, and rendered plists are ignored.
- The runtime uses only Python's standard library.
- The combined local suite contains 87 tests, including fake App Server, supervisor, notification, recovery, redaction, and launchd staging coverage.
- Private GitHub Actions pass on sanitized `main` for Python 3.9, 3.11, and 3.13, plus the separate `Public safety` job.
- Public distribution will be a source checkout and GitHub-generated source archive. No package or installer will be published.

Live GitHub read-back on 2026-08-30 confirms:

- The repository is private, `main` is the default branch, and no tag or release exists.
- The release-candidate repository starts from one sanitized snapshot without the predecessor repository's pull-request or development history.
- GitHub detects Apache-2.0.
- The approved description and eight topics are present; the homepage is empty.
- Issues are enabled. Projects, Wiki, and Discussions are disabled.
- Private branch protection and rulesets remain unavailable on the current plan (`403`). Private vulnerability reporting remains unavailable (`404`), and `security_and_analysis` is not exposed in the private state. These controls remain part of the public cut.

## Interface and support boundary

The public contract is operator-neutral. `operator_attested_subscription_only` is an explicit operator assertion, not machine proof that paid credits are absent. The assertion is bound to the local account fingerprint and plan checks; any conflicting signal fails closed.

The controller can prevent intentional dispatch when supported signals show unsafe headroom. It cannot predict final turn usage, make interruption instantaneous, isolate account-level deltas from concurrent Codex activity, or replace account-level billing controls.

The optional supervisor cannot create approval or change a stored package. It runs in the foreground, owns a singleton lease, dispatches at most one eligible package per cycle, and moves uncertain work to review without automatic retry. LaunchAgent activation is outside the public-alpha claim.

The App Server allowlists and field names were checked against current official Codex documentation on 2026-08-30. Repeat the check at the public cut if the release candidate or official documentation changes. A documentation match does not replace the fake-server suite or a separately approved read-only live canary.

## Completed private preparation gate

The following checks passed while the repository remained private:

1. Run compilation, the full suite, JSON validation, Markdown link checks, plist rendering and lint, public-safety checks over tracked files and history, and `git diff --check`.
2. Run the documented quick start from a fresh clone with no existing scheduler state.
3. Push the sanitized snapshot while the repository remains private.
4. Obtain successful GitHub Actions results on the sanitized default-branch commit and record the actual check context names: `Python 3.9`, `Python 3.11`, `Python 3.13`, and `Public safety`.
5. Repeat the private default-branch and clean-clone read-back on that exact commit.
6. Confirm GitHub detects Apache-2.0.
7. Apply and read back the approved private-safe description, topics, Issues/Wiki/Discussions/Projects settings, and empty homepage.
8. Repeat the tracked-file and public-surface audit on the exact private default-branch commit.

CodeQL is deferred unless it becomes available and adds useful signal. The repository has no third-party Python manifest, so dependency automation would currently imply a dependency surface that does not exist.

## Final public-alpha release gate

The release cut combines public visibility with one GitHub alpha prerelease. It must be one bounded, separately approved window:

1. Name the alpha version and confirm it matches the source version and release notes.
2. Confirm the exact private `main` commit and successful required checks.
3. Confirm Apache-2.0 detection and the approved repository metadata.
4. Change repository visibility to public.
5. Enable and read back private vulnerability reporting, secret scanning, push protection, and other available public security settings.
6. Protect `main` with the observed successful CI contexts, conversation resolution, and force-push and deletion blocks. Choose administrator bypass deliberately.
7. Verify anonymous repository access, clone, README rendering, local links, security policy, issue form, and clean-checkout quick start.
8. Create the approved annotated tag and GitHub alpha prerelease for the exact verified commit. Attach no custom binary or package artifacts; use only GitHub-generated source archives.
9. Read back the tag target, prerelease flag, release notes, and asset set.

Do not announce the project in this window. If a material leak, missing license, broken setup, incorrect tag target, unsafe service behavior, or failed security control appears, stop. Restore private visibility if that rollback is still available, and delete only a newly created incorrect release or tag when the final approval explicitly authorizes that rollback.

## Public-alpha acceptance checklist

- [x] Operator-neutral public documentation prepared.
- [x] Apache-2.0 approved, merged, and detected by GitHub.
- [x] Security, support, and contribution policy files prepared.
- [x] Background supervisor integrated, disabled by default, and documented without launchd activation claims.
- [x] Current official Codex interface review completed on the release candidate.
- [x] Clean-checkout quick start and plist staging checks passed on the integrated release candidate.
- [x] GitHub Actions passed on sanitized private `main`; actual check contexts recorded.
- [x] Tracked-file, snapshot-history, and public-surface hygiene checks passed on the private release candidate.
- [x] Apache-2.0 detection and approved private-safe metadata read back from GitHub.
- [ ] Public security settings and `main` protection applied and read back after visibility changes.
- [x] Alpha limitations and paid-credit guarantee boundary are prominent.
- [ ] Version, release notes, visibility, tag, and prerelease received final approval.
- [ ] Anonymous post-public clone and setup verification passed.

## Exact later approval

Request this bounded approval with the final version and commit filled in. The prepared source and release version is `0.6.0-alpha.1`; the tag remains a release decision:

> Approve making `jremick/codex-work-scheduler` public at commit `<commit>`, applying and verifying the documented public security and `main` protection settings, running anonymous read-back and clean-checkout verification, then creating annotated tag `<version>` and a GitHub alpha prerelease with the reviewed notes and only GitHub-generated source archives. Do not publish a package or announce the project. If a material gate fails, stop; restore private visibility when available, and remove only a newly created incorrect release or tag.

## Deferred beyond public alpha

A launchd installer or activation, network API, external notifications, human UI, new authentication, automatic retry, paid-credit fallback, packaged distribution, stable migrations, and multi-platform support are not part of the public-alpha gate.
