#!/usr/bin/env python3
"""Read-only inventory of managed artifacts on a T3 host."""

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from typing import NamedTuple


FINISHED = {"completed", "interrupted", "failed", "cancelled", "rolled_back"}
MANAGED_GROUPS = ("build/xcode", "build/swiftpm", "runs")


class Snapshot(NamedTuple):
    projects: set
    threads: dict
    active: set
    has_unmapped_legacy_threads: bool
    thread_rows: dict


def overlaps(a, b):
    return a == b or a in b.parents or b in a.parents


def directory(path):
    return path.is_dir() and path.resolve(strict=True) == path


def load_t3(db):
    """Read one consistent snapshot of the installed v2 projection schema."""
    with sqlite3.connect(db.resolve(strict=True).as_uri() + "?mode=ro", uri=True) as conn:
        conn.execute("BEGIN")
        projects = {
            canonical_checkout(path) for (path,) in conn.execute(
                "SELECT workspace_root FROM projection_projects WHERE deleted_at IS NULL"
            )
        }
        threads = defaultdict(list)
        thread_rows = {}
        for tid, payload in conn.execute(
            "SELECT thread_id, payload_json FROM orchestration_v2_projection_threads "
            "WHERE deleted_at IS NULL"
        ):
            row = json.loads(payload)
            thread_rows[tid] = row
            if row["worktreePath"] is not None:
                threads[canonical_checkout(row["worktreePath"])].append((tid, row))
        active = set()
        for table, allowed in (
            ("runs", FINISHED),
            ("runtime_requests", {"resolved", "cancelled"}),
            ("subagents", FINISHED),
        ):
            # Table names are fixed above. Unknown statuses conservatively block review.
            active.update(tid for tid, status in conn.execute(
                f"SELECT thread_id, status FROM orchestration_v2_projection_{table}"
            ) if status not in allowed)
        # A detached session keeps its last status but loses its thread binding.
        # T3 counts only live sessions as open, so an unbound session is not active.
        bound = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table' "
            "AND name = 'orchestration_v2_projection_provider_session_bindings'"
        ).fetchone()[0]
        sessions = (
            "SELECT sessions.thread_id, bindings.thread_id "
            "FROM orchestration_v2_projection_provider_sessions AS sessions "
            "JOIN orchestration_v2_projection_provider_session_bindings AS bindings "
            "ON bindings.provider_session_id = sessions.provider_session_id "
            "WHERE sessions.status != 'stopped'"
        ) if bound else (
            "SELECT thread_id, NULL FROM orchestration_v2_projection_provider_sessions "
            "WHERE status != 'stopped'"
        )
        active.update(tid for row in conn.execute(sessions) for tid in row if tid is not None)
        legacy = conn.execute(
            "SELECT count(*) FROM projection_threads WHERE deleted_at IS NULL AND worktree_path IS NOT NULL "
            "AND thread_id NOT IN (SELECT thread_id FROM orchestration_v2_projection_threads "
            "WHERE deleted_at IS NULL)"
        ).fetchone()[0]
        return Snapshot(projects, threads, active, bool(legacy), thread_rows)


def checkout_state(checkout, worktrees, snapshot):
    if checkout is None:
        return "missing or invalid ownership metadata"
    if any(overlaps(checkout, p) for p in snapshot.projects):
        return "overlaps a T3 project checkout"
    if (snapshot.has_unmapped_legacy_threads or worktrees not in checkout.parents
            or not directory(checkout)):
        return "outside confirmed T3 worktree scope"
    owners = [row for path, rows in snapshot.threads.items()
              if overlaps(path, checkout) for row in rows]
    if not snapshot.threads.get(checkout):
        return "no exact T3 worktree match"
    if any(not row["settledAt"] or row.get("settledOverride") == "active"
           or row.get("pinnedAt") or row.get("snoozedUntil") for _, row in owners):
        return "T3 worktree has an unsettled, pinned or snoozed owner"
    if any(tid in snapshot.active for tid, _ in owners):
        return "T3 worktree has an active run, task or provider session"
    return None


def canonical_checkout(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("checkout is not an absolute path")
    return path.resolve()


def owner(path):
    marker = path / "owner.json"
    if not marker.is_file() or marker.is_symlink():
        return None, {}
    try:
        data = json.loads(marker.read_text())
        return canonical_checkout(data["checkout"]), data
    except (OSError, ValueError, KeyError, TypeError):
        return None, {}


def size_kib(path):
    result = subprocess.run(["/usr/bin/du", "-sk", str(path)], check=True,
                            capture_output=True, text=True, timeout=30)
    return int(result.stdout.split()[0])


def report(root, worktrees, t3):
    managed = root / "workspaces"
    if not directory(managed):
        raise ValueError("managed artifact root missing or symlinked")
    rows = []
    for label in sorted(managed.iterdir()):
        if not directory(label):
            continue
        for workspace in sorted(label.iterdir()):
            if not directory(workspace):
                continue
            checkout, _ = owner(workspace)
            reason = checkout_state(checkout, worktrees, t3)
            for kind in MANAGED_GROUPS:
                group = workspace / kind
                if not directory(group):
                    continue
                for path in sorted(group.iterdir()):
                    if not directory(path):
                        continue
                    block = reason
                    data = {}
                    if kind == "runs":
                        run_checkout, data = owner(path)
                        if run_checkout != checkout or not data.get("session_id"):
                            block = "run ownership is missing or does not match the workspace"
                        elif not block and data.get("t3_thread_id") not in {
                            tid for tid, _ in t3.threads[checkout]
                        }:
                            block = "run lacks a verified T3 thread ID for this checkout"
                    rows.append({
                        "path": str(path), "checkout": str(checkout) if checkout else None,
                        "kind": kind,
                        "session": data.get("session_id"), "allocated_kib": size_kib(path),
                        "status": "keep" if block else "review",
                        "reason": block or "T3 owners appear settled; verify all writers and references before approval",
                    })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--worktrees", type=Path, default=Path.home() / "workspace/worktrees")
    parser.add_argument("--t3-db", type=Path, default=Path.home() / ".t3/userdata/statev2.sqlite")
    parser.add_argument("--report-file", type=Path, help="Atomically replace a single preview report")
    args = parser.parse_args()
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read-only",
        "note": "Review is preliminary. Open files, live terminals, external paths and deletion are outside this tool.",
    }
    try:
        if not directory(args.worktrees):
            raise ValueError("T3 worktree root missing or symlinked")
        result["entries"] = report(args.root, args.worktrees, load_t3(args.t3_db))
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, RuntimeError,
            subprocess.SubprocessError) as error:
        result["error"] = f"Artifact review unavailable: {error}"
    if args.report_file:
        target = args.report_file
        if not directory(target.parent) or target.is_symlink():
            parser.error("report path must be in an existing directory, without symlinks")
        with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, delete=False) as stream:
            try:
                json.dump(result, stream, indent=2)
                stream.write("\n")
                stream.close()
                os.replace(stream.name, target)
            finally:
                Path(stream.name).unlink(missing_ok=True)
    elif "error" in result:
        print(result["error"], file=sys.stderr)
    else:
        print(json.dumps(result, indent=2))
    return int("error" in result)


if __name__ == "__main__":
    sys.exit(main())
