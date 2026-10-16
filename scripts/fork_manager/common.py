import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class Failure(Exception):
    pass


def run(args, *, cwd=ROOT, env=None, capture=False, check=True):
    result = subprocess.run(
        [str(a) for a in args],
        check=False,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE if capture else sys.stderr,
        stderr=subprocess.PIPE if capture else sys.stderr,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "See command output above").strip()
        raise Failure(f"{args[0]} exited {result.returncode}: {detail}")
    return result.stdout.strip() if capture and check else result


def git(*args, cwd=ROOT):
    return run(["git", *args], cwd=cwd, capture=True)


def config(root=ROOT):
    return json.loads((root / "fork-config.json").read_text())


def state_root():
    return (
        Path(os.environ.get("CODEX_FORK_HOME", Path.home() / ".local/share/codex-fork"))
        .expanduser()
        .resolve()
    )


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out, indent=2)
            out.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def package_hash(path):
    """Bind receipts to every shipped file, not just the CLI version string."""
    manifest = path / "codex-package.json"
    if not manifest.is_file() or not (path / "bin/codex").is_file():
        raise Failure(f"Not a complete Codex package: {path}")
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise Failure(f"Package contains a symlink: {item}")
        if item.is_file():
            digest.update(str(item.relative_to(path)).encode() + b"\0")
            with item.open("rb") as stream:
                digest.update(hashlib.file_digest(stream, "sha256").digest())
    return digest.hexdigest()


def receipt_path(package):
    return package.parent / f"{package.name}.verification.json"


def clean(root):
    if git("status", "--porcelain", cwd=root):
        raise Failure(
            f"Worktree has changes; preserve or commit them before continuing: {root}"
        )


@contextmanager
def installation_lock():
    state = state_root()
    state.mkdir(parents=True, exist_ok=True)
    with (state / "installation.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Failure("Another activation or rollback is running") from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
