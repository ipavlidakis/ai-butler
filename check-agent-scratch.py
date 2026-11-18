#!/usr/bin/python3
"""Exercise scratch cleanup with disposable fixtures and real process checks."""

import argparse
from datetime import datetime, timezone
from importlib.machinery import SourceFileLoader
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_loader("scratch", SourceFileLoader(
    "scratch", str(Path(__file__).with_name("ai-butler"))))
scratch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scratch)

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("fixture_parent", type=Path)
parser.add_argument("--live-lsof", action="store_true")
args = parser.parse_args()
real_run = subprocess.run


def fixture_process_check(command, **kwargs):
    if command[0] == "/usr/sbin/lsof" and not any(Path(command[-1]).rglob("open-fixture")):
        return subprocess.CompletedProcess(command, 1, b"", b"")
    return real_run(command, **kwargs)


with tempfile.TemporaryDirectory(dir=args.fixture_parent) as temp:
    base = Path(temp)
    scratch.HOME = base
    scratch.CONFIG = base / "config" / "config.json"
    scratch.AUDIT_DIR = base / "reports" / "audit"
    scratch.REPORT = scratch.AUDIT_DIR.parent / "latest.json"
    scratch.ROOT = base / "disposable-scratch"
    scratch.ROOT.mkdir()
    scratch.WORKTREES = base / "worktrees"
    checkout = scratch.WORKTREES / "demo" / "feature"
    checkout.mkdir(parents=True)
    scratch.DB = base / "test.sqlite"
    cfg = scratch.settings()
    cfg["settlement_grace_seconds"] = 60
    scratch.ensure_directory(scratch.CONFIG.parent)
    scratch.write_json(scratch.CONFIG, cfg)
    conn = sqlite3.connect(scratch.DB)
    conn.executescript("""
        CREATE TABLE projection_projects(workspace_root TEXT, deleted_at TEXT);
        CREATE TABLE projection_threads(thread_id TEXT, worktree_path TEXT, deleted_at TEXT);
        CREATE TABLE orchestration_v2_projection_threads(thread_id TEXT, payload_json TEXT, deleted_at TEXT);
    """)
    for table in ("runs", "runtime_requests", "subagents", "provider_sessions"):
        conn.execute(f"CREATE TABLE orchestration_v2_projection_{table}(thread_id TEXT,status TEXT)")
    def register_thread(tid, settled, path=checkout, parent=None):
        conn.execute("DELETE FROM orchestration_v2_projection_threads WHERE thread_id = ?", (tid,))
        conn.execute("INSERT INTO orchestration_v2_projection_threads VALUES (?, ?, NULL)",
                     (tid, json.dumps({"worktreePath": str(path) if path else None,
                                       "settledAt": settled, "lineage": {"parentThreadId": parent}})))
        conn.commit()
    settled_at = datetime.fromtimestamp(time.time() - 120, timezone.utc).isoformat()
    register_thread("demo", None)
    os.chdir(checkout)
    cmd = [sys.executable, "-c",
           'from pathlib import Path; import os; Path(os.environ["TMPDIR"], "example").touch()']
    assert scratch.run(argparse.Namespace(thread="demo", pool=None, command=cmd)) == 0
    job, = (scratch.ROOT / "tmp").iterdir()
    data = job / "data"
    owner = scratch.read_local_json(job / "owner.json")
    owner["finished_at"] = time.time() - 90
    scratch.write_json(job / "owner.json", owner)
    def expect(mode, status, reason=None):
        row, = scratch.sweep(cfg, mode, sweep_id="fixture")["entries"]
        assert row["status"] == status, row
        if reason:
            assert reason in row["reason"], row
    expect(True, "keep", "not settled")
    register_thread("demo", settled_at)
    register_thread("child", None)
    expect(True, "keep", "not settled")
    register_thread("child", settled_at)
    register_thread("indirect", None, None, "child")
    expect(True, "keep", "not settled")
    register_thread("indirect", settled_at, None, "child")
    conn.execute("INSERT INTO orchestration_v2_projection_runs VALUES ('child', 'queued')")
    conn.commit()
    expect(True, "keep", "active")
    conn.execute("DELETE FROM orchestration_v2_projection_runs")
    conn.commit()
    with scratch.lock(job):
        expect(True, "keep", "lock")
    with (data / "example").open():
        expect(True, "keep", "open files")
    scratch.DB = base / "missing.sqlite"
    expect(True, "keep")
    scratch.DB = base / "test.sqlite"
    untouched = base / "untouched"
    untouched.mkdir()
    (untouched / "keep").touch()
    (data / "alias").symlink_to(untouched)
    def remove_with_audit():
        expect(False, "eligible")
        with patch.object(scratch, "audit", side_effect=OSError("test audit write failed")):
            expect(True, "keep", "test audit write failed")
        assert job.exists()
        expect(True, "deleted")
        events = [json.loads(line) for path in sorted(scratch.AUDIT_DIR.glob("*.jsonl"))
                  for line in path.read_text().splitlines()]
        assert [event["event"] for event in events] == ["delete_intent", "deleted"]
        assert all(event["path"] == str(job) and event["thread"] == "demo" for event in events)
    if args.live_lsof:
        remove_with_audit()
    else:
        with patch.object(subprocess, "run", fixture_process_check):
            remove_with_audit()
    assert not job.exists() and (untouched / "keep").exists() and checkout.exists()

    # The startup hook opts in new workspaces; the ordinary build writes to its assigned path.
    cfg["build_artifacts_root"] = str(base / "artifacts")
    scratch.write_json(scratch.CONFIG, cfg)
    legacy = Path(cfg["build_artifacts_root"]) / "workspaces/legacy/unknown"
    (legacy / "build").mkdir(parents=True)
    (legacy / "build/keep").touch()
    scratch.write_json(legacy / "owner.json", {"checkout": str(checkout)})
    from contextlib import redirect_stdout
    from io import StringIO
    output = StringIO()
    with redirect_stdout(output):
        assert scratch.artifact_hook(argparse.Namespace(
            provider="other", cwd=str(checkout), session="fixture")) == 0
    env = json.loads(output.getvalue())["env"]
    workspace = Path(env["AGENT_WORKSPACE_ARTIFACTS"])
    build = workspace / "build"
    artifact = Path(env["AGENT_XCODE_DIR"]) / "fixture.o"
    artifact.touch()
    result = Path(env["AGENT_RESULTS_DIR"]) / "keep.xcresult"
    result.mkdir()
    (checkout / ".build").symlink_to(build / "swiftpm", target_is_directory=True)

    def builds(apply, status, reason=None):
        rows = scratch.sweep(cfg, apply, sweep_id="build-fixture")["entries"]
        by_path = {row["path"]: row for row in rows}
        assert by_path[str(legacy / "build")]["status"] == "keep"
        row = by_path[str(build)]
        assert row["status"] == status, row
        if reason:
            assert reason in row["reason"], row
        return row

    register_thread("child", None)
    builds(True, "keep", "not settled")
    register_thread("child", settled_at)
    register_thread("indirect", None, None, "child")
    builds(True, "keep", "not settled")
    register_thread("indirect", settled_at, None, "child")
    with patch.object(scratch.review, "load_t3", side_effect=sqlite3.OperationalError("fixture unavailable")):
        builds(True, "keep", "fixture unavailable")
    protect = base / "main"
    protect.mkdir()
    (protect / "DerivedData").symlink_to(build, target_is_directory=True)
    conn.execute("INSERT INTO projection_projects VALUES (?,NULL)", (str(protect),))
    conn.commit()
    builds(True, "keep", "another T3 checkout")
    (protect / "DerivedData").unlink()
    embedded_evidence = artifact.parent / "keep.xcresult"
    embedded_evidence.mkdir()
    builds(True, "keep", "test result")
    embedded_evidence.rmdir()
    (artifact.parent / "open-fixture").touch()
    with (artifact.parent / "open-fixture").open():
        builds(True, "keep", "open files")
    (artifact.parent / "open-fixture").unlink()

    def remove_build():
        builds(False, "eligible")
        with patch.object(scratch, "audit", side_effect=OSError("test audit write failed")):
            builds(True, "keep", "test audit write failed")
        with patch.object(scratch, "audit", wraps=scratch.audit) as audit:
            def resumed(event, sweep_id, **kwargs):
                if event == "delete_intent":
                    register_thread("child", None)
            audit.side_effect = resumed
            builds(True, "keep", "not settled")
        register_thread("child", settled_at)
        old_audit = scratch.audit
        real_check = scratch.open_or_mounted
        def resume_after_rename(path, **kwargs):
            if path.name.startswith(".retired-build-"):
                register_thread("child", None)
            return real_check(path, **kwargs)
        with patch.object(scratch, "open_or_mounted", side_effect=resume_after_rename), \
                patch.object(scratch, "audit", wraps=old_audit) as audit:
            builds(True, "keep", "not settled")
            assert artifact.exists()
            assert audit.call_args_list[-1].args[0] == "delete_cancelled"
        register_thread("child", settled_at)
        row = builds(True, "deleted")
        assert not Path(row["staged_path"]).exists()
        events = [json.loads(line) for path in scratch.AUDIT_DIR.glob("*.jsonl")
                  for line in path.read_text().splitlines()]
        assert [event["event"] for event in events if event.get("kind") == "build"] == [
            "delete_intent", "delete_cancelled", "delete_intent", "deleted"]
    if args.live_lsof:
        remove_build()
    else:
        with patch.object(subprocess, "run", fixture_process_check):
            remove_build()
    assert not build.exists() and result.is_dir() and (legacy / "build/keep").exists()
    assert (workspace / "owner.json").exists() and (checkout / ".build").is_symlink()
    with redirect_stdout(StringIO()):
        scratch.artifact_hook(argparse.Namespace(provider="other", cwd=str(checkout), session="fixture"))
    assert (build / "swiftpm").is_dir() and (checkout / ".build").is_dir()

    # Project cleanup: stray tool output outside the workspace, attributed by the worktree it names.
    stray = base / "stray"
    stray.mkdir()
    for bad in ("/skip-*.txt", str(stray / "*.t"), str(scratch.WORKTREES / "demo/x-*.txt"),
                str(stray) + "/../stray/skip-*.txt"):
        try:
            scratch.configure_project(cfg, "demo", bad, True)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted unsafe pattern {bad}")
    scratch.configure_project(cfg, "demo", str(stray / "skip-export-*.txt"), True)
    assert scratch.read_local_json(scratch.CONFIG)["project_cleanup"] == {
        "demo": [str(stray / "skip-export-*.txt")]}
    old = time.time() - 120

    def stray_file(name, text, mtime=old):
        path = stray / name
        path.write_text(text)
        os.utime(path, (mtime, mtime))
        return path
    owned = stray_file("skip-export-owned.txt", f"--package-path {checkout}/Packages/A\n{checkout}/B\n")
    anonymous = stray_file("skip-export-anon.txt", "no owner here\n")
    ambiguous = stray_file("skip-export-two.txt", f"{checkout}\n{scratch.WORKTREES / 'demo/other'}\n")
    orphan = stray_file("skip-export-orphan.txt", f"{scratch.WORKTREES / 'demo/removed'}/x\n")
    unowned_checkout = scratch.WORKTREES / "demo/unowned"
    unowned_checkout.mkdir()
    unowned = stray_file("skip-export-unowned.txt", f"{unowned_checkout}/x\n")
    unrelated = stray_file("unrelated.txt", f"{checkout}\n")

    def strays(apply):
        rows = scratch.sweep(cfg, apply, sweep_id="project-fixture")["entries"]
        return {Path(r["path"]).name: r for r in rows if r.get("kind") == "project-file"}

    def expect_stray(apply, name, status, reason=None):
        row = strays(apply)[name]
        assert row["status"] == status, row
        if reason:
            assert reason in row["reason"], row
    rows = strays(False)
    assert "unrelated.txt" not in rows
    assert rows["skip-export-anon.txt"]["status"] == "keep"
    assert "exactly one" in rows["skip-export-two.txt"]["reason"]
    assert "without T3 ownership" in rows["skip-export-unowned.txt"]["reason"]
    assert rows["skip-export-orphan.txt"]["status"] == "eligible"
    assert rows["skip-export-owned.txt"]["status"] == "eligible", rows["skip-export-owned.txt"]
    register_thread("indirect", None, None, "child")
    expect_stray(True, "skip-export-owned.txt", "keep", "not settled")
    register_thread("indirect", settled_at, None, "child")
    os.utime(owned, None)
    expect_stray(True, "skip-export-owned.txt", "keep", "grace")
    os.utime(owned, (old, old))
    with owned.open():
        expect_stray(True, "skip-export-owned.txt", "keep", "open file")
    with patch.object(scratch, "audit", side_effect=OSError("test audit write failed")):
        expect_stray(True, "skip-export-owned.txt", "keep", "test audit write failed")
    rows = strays(True)
    assert rows["skip-export-owned.txt"]["status"] == "deleted"
    assert not owned.exists() and not orphan.exists()  # The orphan went on the first applied sweep.
    assert anonymous.exists() and ambiguous.exists() and unowned.exists() and unrelated.exists()
    events = [json.loads(line) for path in scratch.AUDIT_DIR.glob("*.jsonl")
              for line in path.read_text().splitlines()]
    for path in (owned, orphan):
        assert [e["event"] for e in events if e.get("path") == str(path)] == ["delete_intent", "deleted"]
    scratch.configure_project(cfg, "demo", str(stray / "skip-export-*.txt"), False)
    assert "project_cleanup" not in scratch.read_local_json(scratch.CONFIG)
    conn.close()
    os.chdir(base.parent)
print("PASS: opted-in allocation, settlement, shared checkout, queued run, lock, open file, T3 unavailable, preview, and scoped removal")
print("PASS: hook-allocated build cache, shared owners, evidence, aliases, audit and resumed threads")
print("PASS: project stray files: unsafe patterns, owner attribution, orphans, settlement, grace, open file, audit")
