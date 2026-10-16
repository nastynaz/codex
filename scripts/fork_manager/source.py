import json
import os
import re
import sys
import tempfile
from pathlib import Path

from .common import Failure, atomic_json, clean, config, git, run, state_root

LIB_FILTER = "test(heartbeat) | test(steer_preserves_request_origin) | test(client_mcp_rebinding) | test(local_daemon_registers_approval_gated)"
INTEGRATION_FILTER = "test(turn_heartbeat) | test(turn_start_if_idle) | test(automatic_plan_rejections) | test(legacy_permission_profile_requests_fail_closed) | test(responses_lite::base_instructions_use_input_items)"


def check(root):
    run(
        [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "scripts/fork_manager",
            "-p",
            "test_*.py",
        ],
        cwd=root,
    )
    run(["git", "diff", "--check"], cwd=root)
    env = {**os.environ, "RUST_MIN_STACK": "8388608"}
    for packages, target, expression in [
        (["codex-tui", "codex-core"], ["--lib"], LIB_FILTER),
        (["codex-app-server", "codex-core"], ["--test", "all"], INTEGRATION_FILTER),
    ]:
        args = [
            "cargo",
            "nextest",
            "run",
            "--no-fail-fast",
            "--retries",
            "0",
            "--test-threads",
            "2",
        ]
        for package in packages:
            args.extend(["-p", package])
        run([*args, *target, "-E", expression], cwd=root / "codex-rs", env=env)
    return {
        "status": "checks_passed",
        "source": git("rev-parse", "HEAD", cwd=root),
        "live_backend_tested": False,
    }


def latest_tag(cfg):
    releases = json.loads(
        run(
            [
                "gh",
                "release",
                "list",
                "--repo",
                cfg["upstream_repository"],
                "--exclude-drafts",
                "--exclude-pre-releases",
                "--limit",
                "100",
                "--json",
                "tagName",
            ],
            capture=True,
        )
    )
    tags = [
        item["tagName"]
        for item in releases
        if re.fullmatch(r"rust-v\d+\.\d+\.\d+", item["tagName"])
    ]
    if not tags:
        raise Failure(
            "No stable Rust release found among the latest 100 non-prerelease releases"
        )
    return max(
        tags, key=lambda tag: tuple(map(int, tag.removeprefix("rust-v").split(".")))
    )


def prepare(root, tag=None, *, in_place=False):
    """Merge only in a fresh worktree or explicitly disposable CI checkout."""
    if in_place and (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or Path(os.environ.get("GITHUB_WORKSPACE", "")).resolve() != root.resolve()
    ):
        raise Failure("--ci-in-place is restricted to the GitHub Actions workspace")
    clean(root)
    cfg = config(root)
    tag = tag or latest_tag(cfg)
    if not re.fullmatch(r"rust-v\d+\.\d+\.\d+", tag):
        raise Failure(f"Invalid stable release tag: {tag}")
    run(
        [
            "git",
            "fetch",
            "--no-tags",
            f"https://github.com/{cfg['upstream_repository']}.git",
            f"refs/tags/{tag}",
        ],
        cwd=root,
    )
    upstream = git("rev-parse", "FETCH_HEAD^{commit}", cwd=root)
    old = cfg["upstream_base"]
    if upstream == old:
        return {"status": "up_to_date", "tag": tag, "worktree": str(root)}
    if cfg.get("upstream_tag"):
        numbers = lambda value: tuple(map(int, value.removeprefix("rust-v").split(".")))
        if numbers(tag) <= numbers(cfg["upstream_tag"]):
            raise Failure(
                f"Refusing non-forward release update from {cfg['upstream_tag']} to {tag}"
            )
    original = git("rev-parse", "HEAD", cwd=root)
    target = root
    if not in_place:
        parent = state_root() / "worktrees"
        parent.mkdir(parents=True, exist_ok=True)
        target = Path(tempfile.mkdtemp(prefix="sync-", dir=parent)) / "source"
        run(["git", "worktree", "add", "--detach", target, original], cwd=root)
    result = run(
        ["git", "merge", "--no-ff", "--no-commit", upstream], cwd=target, check=False
    )
    if result.returncode:
        conflicts = git(
            "diff", "--name-only", "--diff-filter=U", cwd=target
        ).splitlines()
        return {
            "status": "conflicts",
            "tag": tag,
            "upstream": upstream,
            "previous_head": original,
            "worktree": str(target),
            "conflicts": conflicts,
            "next": "Resolve the listed files in this retained worktree, then run codex-fork finish-sync --source PATH --tag TAG --upstream SHA and codex-fork check --source PATH.",
        }
    return finish(target, tag, upstream, original)


def finish(root, tag, upstream, previous_head=None):
    if git("diff", "--name-only", "--diff-filter=U", cwd=root):
        raise Failure("Unresolved merge conflicts remain")
    upstream = git("rev-parse", f"{upstream}^{{commit}}", cwd=root)
    merge_head = git("rev-parse", "--verify", "MERGE_HEAD", cwd=root)
    if merge_head != upstream:
        raise Failure("The active merge does not match the requested upstream commit")
    if not re.fullmatch(r"rust-v\d+\.\d+\.\d+", tag):
        raise Failure("A stable rust-vX.Y.Z tag is required")
    cfg = config(root)
    cfg.update(upstream_base=upstream, upstream_tag=tag)
    atomic_json(root / "fork-config.json", cfg)
    run(["git", "add", "fork-config.json"], cwd=root)
    run(["git", "commit", "-m", f"fork: Integrate upstream {tag}"], cwd=root)
    return {
        "status": "candidate_ready",
        "tag": tag,
        "upstream": upstream,
        "previous_head": previous_head,
        "head": git("rev-parse", "HEAD", cwd=root),
        "worktree": str(root),
        "next": f"codex-fork check --source {root}",
    }


def publish(root):
    clean(root)
    cfg = config(root)
    details = json.loads(
        run(
            ["gh", "repo", "view", cfg["repository"], "--json", "isFork,parent"],
            capture=True,
        )
    )
    parent = details.get("parent") or {}
    parent_name = (
        parent.get("nameWithOwner")
        or f"{parent.get('owner', {}).get('login')}/{parent.get('name')}"
    )
    if not details.get("isFork") or parent_name != cfg["upstream_repository"]:
        raise Failure(
            "Destination must be your fork of the configured upstream repository"
        )
    check(root)
    url = f"git@github.com:{cfg['repository']}.git"
    # A normal push: never overwrite existing remote history.
    run(["git", "push", url, f"HEAD:refs/heads/{cfg['branch']}"], cwd=root)
    run(
        [
            "gh",
            "repo",
            "edit",
            cfg["repository"],
            "--default-branch",
            cfg["branch"],
            "--enable-issues",
        ]
    )
    workflows = json.loads(
        run(
            [
                "gh",
                "workflow",
                "list",
                "--repo",
                cfg["repository"],
                "--all",
                "--limit",
                "200",
                "--json",
                "id,path,state",
            ],
            capture=True,
        )
    )
    for workflow in workflows:
        if (
            Path(workflow["path"]).name != "fork-maintenance.yml"
            and workflow["state"] == "active"
        ):
            run(
                [
                    "gh",
                    "workflow",
                    "disable",
                    str(workflow["id"]),
                    "--repo",
                    cfg["repository"],
                ]
            )
    run(
        [
            "gh",
            "workflow",
            "enable",
            "fork-maintenance.yml",
            "--repo",
            cfg["repository"],
        ]
    )
    run(
        [
            "gh",
            "workflow",
            "run",
            "fork-maintenance.yml",
            "--repo",
            cfg["repository"],
            "--ref",
            cfg["branch"],
        ]
    )
    return {
        "status": "published",
        "repository": cfg["repository"],
        "branch": cfg["branch"],
        "next": "gh run list --repo "
        + cfg["repository"]
        + " --workflow fork-maintenance.yml",
    }
