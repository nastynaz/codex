import argparse
import json
from pathlib import Path

from . import packages, source
from .common import ROOT, Failure, config, git, state_root


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Maintain the heartbeat fork. update builds and tests; only activate/rollback --yes change the installed Codex."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "status", help="Read configuration and local candidate/activation state"
    )
    sync = sub.add_parser(
        "sync",
        help="Prepare an upstream-release candidate; preserve conflicts in its worktree",
    )
    sync.add_argument("--source", type=Path, default=ROOT)
    sync.add_argument("--tag")
    sync.add_argument(
        "--ci-in-place",
        action="store_true",
        help="Use only in a disposable CI checkout",
    )
    finish = sub.add_parser(
        "finish-sync", help="Commit a manually resolved upstream merge"
    )
    finish.add_argument("--source", type=Path, required=True)
    finish.add_argument("--tag", required=True)
    finish.add_argument("--upstream", required=True)
    for name in ["check", "publish"]:
        p = sub.add_parser(name)
        p.add_argument("--source", type=Path, default=ROOT)
    for name in ["build", "update"]:
        p = sub.add_parser(
            name,
            help="Build a complete versioned package"
            if name == "build"
            else "Fetch maintained source, check, build and live-test; never install",
        )
        p.add_argument("--source", type=Path, default=ROOT)
        p.add_argument(
            "--profile", choices=["release", "dev-small", "dev"], default="release"
        )
        if name == "update":
            p.add_argument(
                "--local",
                action="store_true",
                help="Use local HEAD before publishing the fork",
            )
            p.add_argument("--model")
    for name in ["smoke", "activate"]:
        p = sub.add_parser(name)
        p.add_argument("--package", type=Path)
        if name == "smoke":
            p.add_argument("--model")
        else:
            p.add_argument(
                "--yes",
                action="store_true",
                help="Authorize replacing the launcher and restarting the daemon",
            )
    sub.add_parser("rollback").add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            result = {
                "status": "configured",
                "source": str(ROOT),
                "head": git("rev-parse", "HEAD"),
                "config": config(),
                "state_directory": str(state_root()),
            }
            for name in ["candidate", "active", "activation-pending"]:
                p = state_root() / f"{name}.json"
                if p.exists():
                    result[name] = json.loads(p.read_text())
        elif args.command == "sync":
            result = source.prepare(
                args.source.resolve(), args.tag, in_place=args.ci_in_place
            )
        elif args.command == "finish-sync":
            result = source.finish(args.source.resolve(), args.tag, args.upstream)
        elif args.command in ["check", "publish"]:
            result = getattr(source, args.command)(args.source.resolve())
        elif args.command == "build":
            result = packages.build(args.source.resolve(), profile=args.profile)
        elif args.command == "update":
            result = packages.update(
                args.source.resolve(), args.profile, args.model, args.local
            )
        elif args.command == "smoke":
            result = packages.smoke(
                packages.selected_candidate(args.package), args.model
            )
        elif args.command == "activate":
            result = packages.activate(
                packages.selected_candidate(args.package), yes=args.yes
            )
        else:
            result = packages.rollback(yes=args.yes)
        print(json.dumps(result, indent=2))
        return 3 if result.get("status") == "conflicts" else 0
    except (Failure, OSError, ValueError, KeyError) as error:
        print(
            json.dumps(
                {"status": "failed", "command": args.command, "error": str(error)}
            )
        )
        return 1
