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
    if command[0] == "/usr/sbin/lsof":
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
    conn.close()
    os.chdir(base.parent)
print("PASS: opted-in allocation, settlement, shared checkout, queued run, lock, open file, T3 unavailable, preview, and scoped removal")
