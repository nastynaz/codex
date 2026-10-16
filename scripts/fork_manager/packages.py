import datetime
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from .common import (
    ROOT,
    Failure,
    atomic_json,
    clean,
    config,
    git,
    installation_lock,
    package_hash,
    receipt_path,
    run,
    state_root,
)
from .source import check


def build(root, *, profile="release", ref=None):
    clean(root)
    head = git("rev-parse", f"{ref or 'HEAD'}^{{commit}}", cwd=root)
    state = state_root()
    parent = state / "worktrees"
    parent.mkdir(parents=True, exist_ok=True)
    tree = Path(tempfile.mkdtemp(prefix="build-", dir=parent)) / "source"
    run(["git", "worktree", "add", "--detach", tree, head], cwd=root)
    # Worktree checks must match the source actually being built, not the caller's branch.
    checked = check(tree)
    cfg = config(tree)
    base_version = (cfg.get("upstream_tag") or "rust-v0.0.0").removeprefix("rust-v")
    version = f"{base_version}-naz.{datetime.datetime.now(datetime.UTC):%Y%m%d%H%M%S}.g{head[:10]}"
    package = state / "releases" / version
    package.parent.mkdir(parents=True, exist_ok=True)
    manifest = tree / "codex-rs/Cargo.toml"
    original = manifest.read_text()
    updated, count = re.subn(
        r'(\[workspace\.package\]\s*\nversion\s*=\s*")[^"]+(".*)',
        rf"\g<1>{version}\2",
        original,
        count=1,
    )
    if count != 1:
        raise Failure("Cannot locate the workspace package version")
    manifest.write_text(updated)
    env = {**os.environ, "CODEX_REPO_ROOT": str(tree)}
    env.setdefault("CARGO_TARGET_DIR", str(state / "cargo-target"))
    run(
        [
            sys.executable,
            tree / "scripts/build_codex_package.py",
            "--package-dir",
            package,
            "--cargo-profile",
            profile,
            "--target",
            cfg["target"],
        ],
        cwd=tree,
        env=env,
    )
    actual = run([package / "bin/codex", "--version"], capture=True)
    if version not in actual:
        raise Failure(f"Built CLI version does not match package version: {actual}")
    receipt = {
        "source": head,
        "upstream_base": cfg["upstream_base"],
        "version": version,
        "profile": profile,
        "package_sha256": package_hash(package),
        "checks": checked,
        "live_passed": False,
        "build_worktree": str(tree),
    }
    atomic_json(receipt_path(package), receipt)
    atomic_json(state / "candidate.json", {"package": str(package)})
    return {
        "status": "built",
        "package": str(package),
        "version": version,
        "next": f"codex-fork smoke --package {package}",
    }


def selected_candidate(explicit=None):
    if explicit:
        return Path(explicit).expanduser().resolve()
    path = state_root() / "candidate.json"
    if not path.exists():
        raise Failure("No candidate package; run codex-fork update first")
    return Path(json.loads(path.read_text())["package"])


def verify_receipt(package, *, live=True):
    path = receipt_path(package)
    if not path.is_file():
        raise Failure(f"Missing verification receipt: {path}")
    receipt = json.loads(path.read_text())
    if receipt.get("package_sha256") != package_hash(package):
        raise Failure("Package changed since verification; rebuild it")
    if receipt.get("checks", {}).get("status") != "checks_passed":
        raise Failure("Package has no passing source checks")
    if live and not receipt.get("live_passed"):
        raise Failure("Package needs a live smoke test; run codex-fork smoke")
    return receipt


def smoke(package, model=None):
    receipt = verify_receipt(package, live=False)
    receipt["live_passed"] = False
    atomic_json(receipt_path(package), receipt)
    # Defer this dependency until the live test. Management/help/status need only Python.
    model = model or config()["smoke_model"]
    evidence = state_root() / "evidence" / package.name
    evidence.mkdir(parents=True, exist_ok=True)
    run(
        [
            "uv",
            "run",
            "--no-project",
            "--with",
            "pyte==0.8.2",
            "python",
            ROOT / "scripts/fork_manager/live_smoke.py",
            package / "bin/codex",
            "--model",
            model,
            "--output",
            evidence,
        ]
    )
    result = json.loads((evidence / "result.json").read_text())
    if not result.get("success"):
        raise Failure("Live smoke test did not report success")
    # Detect mutation during the live test as well as before it.
    if receipt["package_sha256"] != package_hash(package):
        raise Failure("Package changed during the smoke test")
    receipt.update(
        live_passed=True,
        live_model=model,
        evidence=str(evidence),
        verified_at=datetime.datetime.now(datetime.UTC).isoformat(),
    )
    atomic_json(receipt_path(package), receipt)
    return {
        "status": "verified",
        "package": str(package),
        "evidence": str(evidence),
        "installed": False,
        "next": "codex-fork activate (preview), then codex-fork activate --yes when sessions may be interrupted",
    }


def update(root, profile="release", model=None, local=False):
    clean(root)
    cfg = config(root)
    ref = "HEAD"
    if not local:
        run(
            [
                "git",
                "fetch",
                "--no-tags",
                f"https://github.com/{cfg['repository']}.git",
                cfg["branch"],
            ],
            cwd=root,
        )
        ref = git("rev-parse", "FETCH_HEAD", cwd=root)
        # Keep the management command itself current, without discarding local work.
        run(["git", "merge", "--ff-only", ref], cwd=root)
    result = build(root, profile=profile, ref=ref)
    return smoke(Path(result["package"]), model)


def _launcher():
    return (
        Path(os.environ.get("CODEX_FORK_LAUNCHER", Path.home() / ".local/bin/codex"))
        .expanduser()
        .absolute()
    )


def _daemon_package():
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    path = (home / "packages/app-server-daemon/current").resolve()
    if not (path / "codex-package.json").is_file():
        raise Failure(
            "Cannot identify the installed daemon package; refusing an activation without a rollback target"
        )
    return path


def _replace_link(launcher, target):
    temporary = launcher.with_name(f".codex-fork-{os.getpid()}")
    if temporary.exists() or temporary.is_symlink():
        raise Failure(f"Temporary launcher path already exists: {temporary}")
    try:
        temporary.symlink_to(target)
        os.replace(temporary, launcher)
    finally:
        temporary.unlink(missing_ok=True)


def activate(package, *, yes=False):
    if yes:
        with installation_lock():
            return _activate(package, yes=True)
    return _activate(package, yes=False)


def _activate(package, *, yes):
    receipt = verify_receipt(package)
    launcher = _launcher()
    if not launcher.is_symlink():
        raise Failure(
            f"Expected an existing symlink at {launcher}; refusing to overwrite a file"
        )
    previous_cli = launcher.resolve()
    previous_daemon = _daemon_package()
    plan = {
        "status": "ready_to_activate",
        "package": str(package),
        "version": receipt["version"],
        "launcher": str(launcher),
        "previous_cli": str(previous_cli),
        "previous_daemon_package": str(previous_daemon),
        "warning": "The shared daemon will restart and may interrupt active sessions.",
    }
    if not yes:
        return {**plan, "next": f"codex-fork activate --package {package} --yes"}
    state = state_root()
    # Exclusive transaction file prevents overlapping activation/rollback and records recovery
    # targets before the first external mutation. Failures retain it for rollback.
    transaction = state / "activation-pending.json"
    state.mkdir(parents=True, exist_ok=True)
    with transaction.open("x") as out:
        json.dump(plan, out)
    run(
        [package / "bin/codex", "app-server", "daemon", "update", "--from-cli", "--yes"]
    )
    if launcher.resolve() != previous_cli:
        raise Failure(
            "Launcher changed during daemon installation. Activation journal retained; run codex-fork rollback."
        )
    _replace_link(launcher, package / "bin/codex")
    atomic_json(state / "active.json", plan)
    transaction.unlink()
    return {**plan, "status": "activated", "next": "codex app-server daemon version"}


def rollback(*, yes=False):
    if yes:
        with installation_lock():
            return _rollback(yes=True)
    return _rollback(yes=False)


def _rollback(*, yes):
    state = state_root()
    journal = state / "activation-pending.json"
    record = journal if journal.exists() else state / "active.json"
    if not record.is_file():
        raise Failure("No activation history is available")
    previous = json.loads(record.read_text())
    cli = Path(previous["previous_cli"])
    daemon = Path(previous["previous_daemon_package"])
    launcher = Path(previous["launcher"])
    if not launcher.is_symlink():
        raise Failure("Launcher is no longer a symlink; refusing to overwrite it")
    permitted = {cli.resolve(), (Path(previous["package"]) / "bin/codex").resolve()}
    if launcher.resolve() not in permitted:
        raise Failure("Launcher was changed externally; inspect it before rolling back")
    if not cli.is_file() or not (daemon / "bin/codex").is_file():
        raise Failure("Previous package files are missing; cannot roll back safely")
    plan = {
        "status": "ready_to_rollback",
        "cli": str(cli),
        "daemon_package": str(daemon),
        "warning": "Rollback restarts the shared daemon; it does not reverse database migrations.",
    }
    if not yes:
        return {**plan, "next": "codex-fork rollback --yes"}
    run([daemon / "bin/codex", "app-server", "daemon", "update", "--from-cli", "--yes"])
    _replace_link(launcher, cli)
    atomic_json(state / "last-rollback.json", previous)
    (state / "active.json").unlink(missing_ok=True)
    journal.unlink(missing_ok=True)
    return {**plan, "status": "rolled_back"}
