# Public release plan

Status: **Stage 2 - Public Alpha**

Target: **Stage 3 - Public Beta**

Last reviewed: **2026-08-31**

This plan records the completed public-alpha release evidence and the approval boundary used for the cut. It does not authorize a future release, package publication, authentication change, or announcement.

## Current public alpha

- The repository explains its safety boundary, alpha limitations, source-checkout quick start, disabled background supervisor, support, security, contribution terms, compatibility, and Apache-2.0 license.
- The background supervisor and launchd staging surface are integrated. The example remains disabled, and the repository has no launchd installation or activation command.
- Runtime state, local configuration, SQLite files, logs, environment files, approvals, and rendered plists are ignored.
- The runtime uses only Python's standard library.
- The `v0.7.0-alpha.1` release suite contains 132 tests, including quota-guard inventory, containment, reset proof, guarded `/goal` resume, task-control ambiguity, controller mode, and dispatch-race coverage.
- GitHub Actions pass on `main` for Python 3.9, 3.11, and 3.13, plus the separate `Public safety` job.
- Public distribution is a source checkout and GitHub-generated source archive. No package or installer is published.

Live post-release GitHub read-back on 2026-08-31 confirms:

- The repository is public and `main` is the default branch.
- Annotated tag `v0.7.0-alpha.1` targets merge commit `58ebc90450adb6a5fc6b32eef5505b046a648a36`. The matching GitHub release is published as a prerelease with no custom assets.
- The alpha release retains only the sanitized release history. The predecessor repository remains a separate private read-only archive.
- GitHub detects Apache-2.0.
- The approved description and eight topics are present; the homepage is empty.
- Issues are enabled. Projects, Wiki, and Discussions are disabled.
- Secret scanning, push protection, vulnerability alerts, and private vulnerability reporting are enabled. The secret-scanning alert count was zero at the cut.
- Classic protection on `main` requires the four observed GitHub Actions checks with strict freshness and conversation resolution. Force pushes and branch deletion are blocked. Administrator emergency bypass remains available for the solo maintainer.
- Dependabot security updates and CodeQL remain deferred because this source-only standard-library project has no third-party package manifest. Projects, Wiki, and Discussions remain disabled.

## Current quota guard alpha

`v0.7.0-alpha.1` adds the disabled-by-default quota guard. The protected pull request and post-merge checks passed, the annotated tag targets the exact verified merge commit, and the GitHub prerelease has no custom assets. The release retains source-only distribution and does not activate background operation or task control.

The live task-control canary is intentionally separate from source publication. Until that canary is approved and passes, the compatibility claim remains synthetic App Server coverage only.

## Interface and support boundary

The public contract is operator-neutral. `operator_attested_subscription_only` is an explicit operator assertion, not machine proof that paid credits are absent. The assertion is bound to the local account fingerprint and plan checks; any conflicting signal fails closed.

The controller can prevent intentional dispatch when supported signals show unsafe headroom. It cannot predict final turn usage, make interruption instantaneous, isolate account-level deltas from concurrent Codex activity, or replace account-level billing controls.

The optional supervisor cannot create approval or change a stored package. It runs in the foreground, owns a singleton lease, dispatches at most one eligible package per cycle, and moves uncertain work to review without automatic retry. LaunchAgent activation is outside the public-alpha claim.

The App Server allowlists and field names were checked against current official Codex documentation on 2026-08-30. Repeat the check before a future release if the source or official documentation changes. A documentation match does not replace the fake-server suite or a separately approved read-only live canary.

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

## Completed public-alpha release gate

The approved release window completed these steps:

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
- [x] Public security settings and `main` protection applied and read back after visibility changes.
- [x] Alpha limitations and paid-credit guarantee boundary are prominent.
- [x] Version, release notes, visibility, tag, and prerelease received final approval.
- [x] Anonymous post-public clone and setup verification passed.

## Completed release authorization

The maintainer approved the bounded quota-guard alpha cut for merge commit `58ebc90450adb6a5fc6b32eef5505b046a648a36` and tag `v0.7.0-alpha.1`. The [GitHub alpha prerelease](https://github.com/jremick/codex-work-scheduler/releases/tag/v0.7.0-alpha.1) uses only GitHub-generated source archives. No package was published and no announcement was made.

The prior `v0.6.0-alpha.1` prerelease remains available as the initial public-alpha baseline.

## Deferred beyond public alpha

A launchd installer or activation, network API, external notifications, human UI, new authentication, automatic retry of uncertain work, paid-credit fallback, packaged distribution, stable migrations, and multi-platform support are not part of the public-alpha gate.
