import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fork_manager import packages, source
from fork_manager.common import (
    Failure,
    atomic_json,
    installation_lock,
    package_hash,
    receipt_path,
)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.launcher = self.root / "codex"
        self.old = self.make_package("old")
        self.new = self.make_package("new")
        self.launcher.symlink_to(self.old / "bin/codex")
        daemon = self.home / "packages/app-server-daemon/current"
        daemon.parent.mkdir(parents=True)
        daemon.symlink_to(self.old, target_is_directory=True)
        self.environment = patch.dict(
            os.environ,
            {
                "CODEX_FORK_HOME": str(self.state),
                "CODEX_HOME": str(self.home),
                "CODEX_FORK_LAUNCHER": str(self.launcher),
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        atomic_json(
            receipt_path(self.new),
            {
                "version": "1.0.0-naz.1",
                "checks": {"status": "checks_passed"},
                "package_sha256": package_hash(self.new),
                "live_passed": True,
            },
        )

    def make_package(self, name):
        path = self.root / name
        (path / "bin").mkdir(parents=True)
        (path / "bin/codex").write_text("fake executable " + name)
        (path / "codex-package.json").write_text('{"version":"1.0.0"}')
        return path

    def test_preview_never_runs_daemon_or_replaces_launcher(self):
        with patch.object(packages, "run") as command:
            result = packages.activate(self.new)
            self.assertEqual(result["status"], "ready_to_activate")
            command.assert_not_called()
        self.assertEqual(self.launcher.resolve(), self.old / "bin/codex")
        self.assertFalse((self.state / "active.json").exists())

    def test_package_tampering_invalidates_receipt(self):
        (self.new / "bin/codex").write_text("changed")
        with patch.object(packages, "run") as command, self.assertRaises(Failure):
            packages.activate(self.new, yes=True)
        command.assert_not_called()

    def test_unverified_package_cannot_activate(self):
        receipt = json.loads(receipt_path(self.new).read_text())
        receipt["live_passed"] = False
        atomic_json(receipt_path(self.new), receipt)
        with self.assertRaises(Failure):
            packages.activate(self.new, yes=True)
        self.assertEqual(self.launcher.resolve(), self.old / "bin/codex")

    def test_activation_and_rollback_restore_distinct_cli_and_daemon(self):
        other_daemon = self.make_package("daemon-old")
        current = self.home / "packages/app-server-daemon/current"
        current.unlink()
        current.symlink_to(other_daemon)
        with patch.object(packages, "run") as command:
            packages.activate(self.new, yes=True)
            self.assertEqual(command.call_args.args[0][0], self.new / "bin/codex")
            self.assertEqual(self.launcher.resolve(), self.new / "bin/codex")
            before = command.call_count
            packages.rollback()
            self.assertEqual(command.call_count, before)
            packages.rollback(yes=True)
            self.assertEqual(command.call_args.args[0][0], other_daemon / "bin/codex")
        self.assertEqual(self.launcher.resolve(), self.old / "bin/codex")

    def test_daemon_failure_preserves_launcher_and_recovery_journal(self):
        with (
            patch.object(packages, "run", side_effect=Failure("install failed")),
            self.assertRaises(Failure),
        ):
            packages.activate(self.new, yes=True)
        self.assertEqual(self.launcher.resolve(), self.old / "bin/codex")
        self.assertTrue((self.state / "activation-pending.json").exists())
        with patch.object(packages, "run"):
            packages.rollback(yes=True)
        self.assertFalse((self.state / "activation-pending.json").exists())

    def test_external_launcher_change_is_not_overwritten_by_rollback(self):
        with patch.object(packages, "run"):
            packages.activate(self.new, yes=True)
        other = self.make_package("unrelated")
        self.launcher.unlink()
        self.launcher.symlink_to(other / "bin/codex")
        with self.assertRaises(Failure), patch.object(packages, "run") as command:
            packages.rollback(yes=True)
        command.assert_not_called()

    def test_installations_are_serialized(self):
        with installation_lock(), self.assertRaises(Failure):
            packages.activate(self.new, yes=True)

    def test_regular_launcher_file_is_never_overwritten(self):
        self.launcher.unlink()
        self.launcher.write_text("user wrapper")
        with self.assertRaises(Failure):
            packages.activate(self.new, yes=True)
        self.assertEqual(self.launcher.read_text(), "user wrapper")

    def test_symlinked_package_resources_are_rejected(self):
        (self.new / "external").symlink_to(self.old / "bin/codex")
        with self.assertRaises(Failure):
            package_hash(self.new)

    def test_local_update_builds_and_smokes_without_activating(self):
        with (
            patch.object(packages, "clean"),
            patch.object(packages, "config", return_value={}),
            patch.object(
                packages, "build", return_value={"package": str(self.new)}
            ) as build,
            patch.object(
                packages, "smoke", return_value={"status": "verified"}
            ) as smoke,
            patch.object(packages, "activate") as activate,
        ):
            self.assertEqual(
                packages.update(self.root, local=True)["status"], "verified"
            )
            build.assert_called_once()
            smoke.assert_called_once_with(self.new, None)
            activate.assert_not_called()

    def test_release_selection_ignores_sdk_tags_and_orders_versions_numerically(self):
        releases = [
            {"tagName": tag}
            for tag in [
                "python-v9.0.0",
                "rust-v0.99.0",
                "rust-v0.163.0",
                "rust-v0.164.0-alpha.1",
            ]
        ]
        with patch.object(source, "run", return_value=json.dumps(releases)):
            self.assertEqual(
                source.latest_tag({"upstream_repository": "test/repo"}), "rust-v0.163.0"
            )


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "file").write_text("base\n")
        self.git("add", "file")
        self.git("commit", "-m", "base")
        self.upstream = self.git("rev-parse", "HEAD")
        self.git("checkout", "-b", "upstream")
        (self.repo / "file").write_text("upstream\n")
        self.git("commit", "-am", "release")
        self.git("tag", "rust-v1.0.0")
        self.git("checkout", "main")
        (self.repo / "fork-config.json").write_text(
            json.dumps(
                {
                    "upstream_repository": "test/repo",
                    "upstream_base": self.upstream,
                    "upstream_tag": None,
                }
            )
        )
        self.git("add", "fork-config.json")
        self.git("commit", "-m", "fork config")
        self.environment = patch.dict(
            os.environ, {"CODEX_FORK_HOME": str(self.base / "state")}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args], cwd=self.repo, stderr=subprocess.DEVNULL, text=True
        ).strip()

    def redirect_fetch(self, args, **kwargs):
        args = list(args)
        if args[:3] == ["git", "fetch", "--no-tags"]:
            args[3] = str(self.repo)
        return self.original_run(args, **kwargs)

    def test_sync_uses_a_candidate_and_preserves_caller(self):
        self.original_run = source.run
        before = self.git("rev-parse", "HEAD")
        with patch.object(source, "run", side_effect=self.redirect_fetch):
            result = source.prepare(self.repo, "rust-v1.0.0")
        self.assertEqual(result["status"], "candidate_ready")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual((self.repo / "file").read_text(), "base\n")
        self.assertEqual((Path(result["worktree"]) / "file").read_text(), "upstream\n")

    def test_conflicts_are_retained_without_advancing_source(self):
        (self.repo / "file").write_text("fork change\n")
        self.git("commit", "-am", "fork change")
        before = self.git("rev-parse", "HEAD")
        self.original_run = source.run
        with patch.object(source, "run", side_effect=self.redirect_fetch):
            result = source.prepare(self.repo, "rust-v1.0.0")
        self.assertEqual(result["status"], "conflicts")
        self.assertEqual(result["conflicts"], ["file"])
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertIn("<<<<<<<", (Path(result["worktree"]) / "file").read_text())

    def test_build_stamps_only_its_worktree_and_binds_receipt_to_package(self):
        cfg_path = self.repo / "fork-config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg.update(upstream_tag="rust-v1.0.0", target="aarch64-apple-darwin")
        cfg_path.write_text(json.dumps(cfg))
        (self.repo / "codex-rs").mkdir()
        manifest = self.repo / "codex-rs/Cargo.toml"
        manifest.write_text('[workspace.package]\nversion = "1.0.0"\n')
        (self.repo / "scripts").mkdir()
        # Exercise the subprocess builder interface without invoking Rust in this unit test.
        (self.repo / "scripts/build_codex_package.py").write_text("""
import argparse, pathlib, json, tomllib
p=argparse.ArgumentParser()
p.add_argument('--package-dir', type=pathlib.Path)
p.add_argument('--cargo-profile')
p.add_argument('--target')
a=p.parse_args()
version=tomllib.loads(pathlib.Path('codex-rs/Cargo.toml').read_text())['workspace']['package']['version']
(a.package_dir/'bin').mkdir(parents=True)
(a.package_dir/'codex-package.json').write_text(json.dumps({'version':version}))
cli=a.package_dir/'bin/codex'
cli.write_text('#!/bin/sh\\necho codex-cli '+version+'\\n')
cli.chmod(0o755)
""")
        self.git(
            "add",
            "fork-config.json",
            "codex-rs/Cargo.toml",
            "scripts/build_codex_package.py",
        )
        self.git("commit", "-m", "package fixture")
        with patch.object(packages, "check", return_value={"status": "checks_passed"}):
            result = packages.build(self.repo, profile="dev")
        self.assertTrue(result["version"].startswith("1.0.0-naz."))
        self.assertIn('version = "1.0.0"', manifest.read_text())
        receipt = packages.verify_receipt(Path(result["package"]), live=False)
        self.assertEqual(receipt["version"], result["version"])
        self.assertFalse(receipt["live_passed"])
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_dirty_worktree_and_non_release_tags_are_rejected(self):
        with self.assertRaises(Failure):
            source.prepare(self.repo, "rust-v1.0.0", in_place=True)
        with self.assertRaises(Failure):
            source.prepare(self.repo, "main")
        (self.repo / "personal.txt").write_text("unrelated work")
        with self.assertRaises(Failure):
            source.prepare(self.repo, "rust-v1.0.0")


if __name__ == "__main__":
    unittest.main()
