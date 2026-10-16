# Maintaining the heartbeat distribution

The maintained source branch is `nastynaz/codex:fork-main`. The original
`naz/cli-heartbeat` branch remains available locally. Three initial feature
commits contain heartbeat delivery/scheduling, request compatibility, and the
footer. Maintenance tooling is a separate commit. Unrelated personal files are
not part of the fork.

`fork-config.json` records the exact upstream base. The bootstrap starts at the
already live-tested development revision; daily maintenance integrates stable
release tags using merge commits. This preserves history and any original
downstream differences rather than rewriting branches beneath other checkouts.
No branch is force-pushed. Keep temporary compatibility fixes separate, and
remove them when an upstream implementation and a live test make them obsolete.

## One-time publication

Create the fork, then publish from the maintained checkout:

```sh
gh repo fork openai/codex --fork-name codex --clone=false --remote=false
codex-fork publish
```

`publish` verifies the repository is your fork, runs the source checks, normally
pushes `fork-main`, sets
it as the default branch, enables issues, disables inherited upstream workflows,
and enables/dispatches only `fork-maintenance.yml`. It does not change the main
Codex launcher, daemon, login or configuration. It needs GitHub repo/workflow
permissions. A failed/non-fast-forward push stops publication without overwriting
remote work. Inspect `gh run list --repo nastynaz/codex --workflow fork-maintenance.yml`
after publication; a workflow file alone is not proof the schedule is running.

The daily 03:37 UTC workflow checks the latest stable upstream release. If it
changed, it prepares an integration candidate and runs the management tests,
heartbeat/recovery unit tests, and request/heartbeat integration tests. Only a
passing candidate advances `fork-main`. No change means no Rust rebuild. The
workflow also supports manual dispatch. Failed merges or checks retain the old
branch, upload diagnostics, and open/update one maintenance issue. An agent can
repair the candidate; it must rerun the same checks. No autonomous repair agent
or personal model credentials are installed in GitHub Actions.

Integration normalizes only local workspace version entries in `Cargo.lock`;
external dependency versions and checksums are preserved. Focused app-server
tests disable host managed-configuration loading so a developer machine's policy
service cannot stall unrelated schema tests. Tests run serially without retries.

## Weekly update

Prerequisites: Git, Python 3.11+, GitHub CLI authentication, Rust/rustup,
`cargo-nextest`, `uv`, and the platform prerequisites of the upstream package
builder. On macOS, install Xcode command-line tools and `protoc`.

```sh
codex-fork status
codex-fork update
```

`update` fetches the maintained branch, fast-forwards the clean management
checkout (refusing local divergence), creates a separate build worktree, runs checks,
builds a complete release package, and runs an isolated live model/TUI test.
It leaves your current installation running. The live test creates a one-minute
one-shot, reopens its TUI, checks the cyan footer and delivery, then verifies
schedule/footer removal. It uses a private temporary copy of your existing
file-based login with the refresh token removed, deletes that copy, and stops its
own server. It cannot rotate your main login's refresh token. If the access token
has expired, refresh your normal Codex login/session and retry. It consumes model
usage. Change the model with `--model MODEL`. Credentials in a keyring without
an `auth.json` file need a separate supported login preparation first.

For a pre-publication check use `codex-fork update --local`. `build` only builds
and checks; `smoke` runs the live test separately. `--profile dev` is useful for
local iteration; ordinary updates use `release`. The version includes the
upstream release, build time and source hash; both the binary and package
manifest use that version. `0.0.0-naz...` identifies the development bootstrap.

State defaults to `~/.local/share/codex-fork`; override it with `CODEX_FORK_HOME`.
Set `CARGO_TARGET_DIR` to reuse a large external build cache. Worktrees are
retained for diagnosis and reproducibility, including the build-time version
stamp. Packages, verification receipts and prior packages are retained. Delete
old worktrees deliberately with `git worktree remove` after inspecting them;
there is no automatic deletion of source worktrees or packages.

## Activate only when ready

```sh
codex-fork activate       # preview only
codex-fork activate --yes # restart daemon and switch ~/.local/bin/codex
```

Activation requires a passing receipt whose hash covers every package file.
It pins the complete package with `app-server daemon update --from-cli`, then
atomically switches the CLI symlink. It can interrupt active/queued sessions.
It refuses to overwrite a regular file at the launcher path. An interrupted
activation leaves a journal with the previous CLI and daemon paths.

```sh
codex-fork rollback       # preview only
codex-fork rollback --yes # restore prior daemon and CLI selection
```

Rollback restarts the daemon. It requires the previous package files to still
exist and does not undo database migrations. Preserve those packages. The
upstream updater is not the fork updater: use `codex-fork update` for this
distribution. `codex-upstream` remains a separate recovery command when installed
by the local bootstrap. Never run activation from scheduled CI.

## Resolve an upstream conflict

```sh
codex-fork sync
```

The JSON result gives the retained worktree, upstream tag/hash and conflicting
paths. Resolve and stage only those paths there. Run the reported `finish-sync`
command, then `codex-fork check --source PATH`. Publish the corrected candidate
with a normal push to `fork-main`; a concurrent remote update will reject the
push and must be reconciled. Keep the recorded upstream base accurate.

Commands return JSON on stdout and diagnostic logs on stderr. Exit 0 means the
requested phase succeeded (including an activation preview); exit 1 is an error,
2 is invalid CLI usage, and 3 is an upstream conflict requiring repair. Source
CI success is distinct from live-backend verification and from activation.
