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
    # Report only the fixture's deliberately opened file, so unrelated processes cannot affect results.
    if command[0] == "/usr/sbin/lsof":
        result = real_run(command, **kwargs)
        kept = [line for line in result.stdout.splitlines()
                if not line.startswith(b"n") or line.endswith(b"/open-fixture")]
        return subprocess.CompletedProcess(command, result.returncode, b"\n".join(kept), result.stderr)
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
    derived = base / "DerivedData"
    scratch.xcode_derived_data = lambda: derived  # Never read the real Xcode folder.
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
    for table in ("runs", "runtime_requests", "subagents"):
        conn.execute(f"CREATE TABLE orchestration_v2_projection_{table}(thread_id TEXT,status TEXT)")
    conn.executescript("""
        CREATE TABLE orchestration_v2_projection_provider_sessions(
            provider_session_id TEXT, thread_id TEXT, status TEXT);
        CREATE TABLE orchestration_v2_projection_provider_session_bindings(
            provider_session_id TEXT, thread_id TEXT);
    """)
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
    # A detached session (ready, no binding) is not live; a bound one is.
    conn.execute("INSERT INTO orchestration_v2_projection_provider_sessions VALUES ('s1', 'child', 'ready')")
    conn.commit()
    expect(False, "eligible")
    conn.execute("INSERT INTO orchestration_v2_projection_provider_session_bindings VALUES ('s1', 'child')")
    conn.commit()
    expect(True, "keep", "active")
    conn.execute("DROP TABLE orchestration_v2_projection_provider_session_bindings")
    conn.commit()
    expect(True, "keep", "active")  # Without the bindings table, every non-stopped session counts.
    conn.execute("DELETE FROM orchestration_v2_projection_provider_sessions")
    conn.execute("CREATE TABLE orchestration_v2_projection_provider_session_bindings("
                 "provider_session_id TEXT, thread_id TEXT)")
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

    # Evidence retention: a run folder goes once it is old enough, its owners settled, and nothing is open.
    run_dir = Path(env["AGENT_RUN_DIR"])
    (run_dir / "logs" / "evidence.log").write_text("proof\n")

    def age(path, days):
        moment = time.time() - days * 86400
        for item in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            os.utime(item, (moment, moment), follow_symlinks=False)
        os.utime(path, (moment, moment))

    def evidence(apply, status, reason=None):
        rows = [r for r in scratch.sweep(cfg, apply, sweep_id="evidence-fixture")["entries"]
                if r.get("kind") == "evidence"]
        row, = rows
        assert row["path"] == str(run_dir) and row["status"] == status, row
        if reason:
            assert reason in row["reason"], row
    assert cfg["evidence_retention_days"] == 7
    evidence(True, "keep", "changed within the last 7 days")
    age(run_dir, 8)
    register_thread("child", None)
    evidence(True, "keep", "not settled")
    register_thread("child", settled_at)
    with (run_dir / "logs" / "evidence.log").open():
        evidence(True, "keep", "open files")
    cfg["evidence_retention_days"] = 0
    assert not [r for r in scratch.sweep(cfg, True, sweep_id="x")["entries"] if r.get("kind") == "evidence"]
    cfg["evidence_retention_days"] = 7
    evidence(False, "eligible")
    assert run_dir.exists()
    evidence(True, "deleted")
    assert not run_dir.exists() and (workspace / "owner.json").exists()
    events = [json.loads(line) for path in scratch.AUDIT_DIR.glob("*.jsonl")
              for line in path.read_text().splitlines()]
    assert [e["event"] for e in events if e.get("path") == str(run_dir)] == ["delete_intent", "deleted"]

    # Xcode DerivedData: folders are attributed to the T3 worktree they were built from.
    import plistlib
    expected_hash = "bpoaeuazmnwwodafvpsgzlsvxefp"  # Observed from Xcode 27.2 for this path.
    assert scratch.xcode_path_hash("/tmp/claude-501/-Users-ipavlidakis--t3-scratch-2026-11-18-go-through-the-t3-"
                                   "thread-81b3deff/835d9395-c074-41a5-b0d5-7c7d7f3d97dd/scratchpad/ddprobe") == expected_hash
    derived.mkdir()
    (derived / "ModuleCache.noindex").mkdir()

    def built(workspace, plist=True, name=None):
        name = name or Path(workspace).stem
        folder = derived / f"{name}-{scratch.xcode_path_hash(str(workspace))}"
        (folder / "Build").mkdir(parents=True)
        (folder / "Build" / "out.o").write_bytes(b"\0" * 4096)
        if plist:
            with (folder / "info.plist").open("wb") as stream:
                plistlib.dump({"WorkspacePath": str(workspace)}, stream)
        return folder

    def derived_rows(apply):
        return {Path(r["path"]).name: r for r in scratch.sweep(cfg, apply, sweep_id="dd-fixture")["entries"]
                if r.get("kind") == "derived-data"}

    app = built(checkout / "App.xcodeproj")
    outside = built(base / "main" / "App.xcodeproj", name="Main")
    mismatched = built(checkout / "Other.xcodeproj")
    mismatched.rename(derived / ("Other-" + "a" * 28))
    mismatched = derived / ("Other-" + "a" * 28)
    orphan = built(scratch.WORKTREES / "demo" / "gone" / "App.xcodeproj", name="Gone")
    package_tree = scratch.WORKTREES / "demo" / "package"
    (package_tree / "Pkg").mkdir(parents=True)
    (package_tree / "Pkg" / "Package.swift").write_text("// swift-tools-version:5.9\n")
    for tree in (package_tree, checkout):
        subprocess.run(["git", "init", "-q", str(tree)], check=True)
    register_thread("pkg", settled_at, package_tree)
    no_plist = built(package_tree / "Pkg", plist=False)
    unmatched = built(package_tree / "Missing", plist=False)
    staged = derived / (scratch.RETIRED_DERIVED + "leftover")
    staged.mkdir()

    register_thread("child", None)
    rows = derived_rows(True)
    assert "ModuleCache.noindex" not in rows
    assert "not settled" in rows[app.name]["reason"], rows[app.name]
    assert "outside the T3 worktrees" in rows[outside.name]["reason"]
    assert "does not match" in rows[mismatched.name]["reason"]
    assert "no single T3 worktree" in rows[unmatched.name]["reason"], rows[unmatched.name]
    assert rows[staged.name]["status"] == "staged"
    assert rows[orphan.name]["status"] == "deleted" and not orphan.exists()
    assert rows[no_plist.name]["status"] == "deleted" and not no_plist.exists()
    register_thread("child", settled_at)
    (app / "Build" / "open-fixture").touch()
    with (app / "Build" / "open-fixture").open():
        assert "open files" in derived_rows(True)[app.name]["reason"]
    assert derived_rows(False)[app.name]["status"] == "eligible" and app.exists()
    row = derived_rows(True)[app.name]
    assert row["status"] == "deleted" and row["checkout"] == str(checkout) and not app.exists()
    assert outside.exists() and mismatched.exists() and unmatched.exists() and staged.exists()
    assert (derived / "ModuleCache.noindex").exists()
    events = [json.loads(line) for path in scratch.AUDIT_DIR.glob("*.jsonl")
              for line in path.read_text().splitlines()]
    for folder in (app, orphan, no_plist):
        assert [e["event"] for e in events if e.get("path") == str(folder)] == ["delete_intent", "deleted"]

    # A gitignored generated workspace (Tuist, XcodeGen) still attributes a folder without info.plist,
    # and the recorded owner survives T3 removing the worktree.
    import shutil
    generated = scratch.WORKTREES / "demo" / "generated"
    (generated / "Gen.xcworkspace").mkdir(parents=True)
    (generated / "Gen.xcworkspace" / "contents.xcworkspacedata").write_text("<Workspace/>\n")
    (generated / ".gitignore").write_text("*.xcworkspace\n")
    subprocess.run(["git", "init", "-q", str(generated)], check=True)
    register_thread("gen", None, generated)
    resolved_only = built(generated / "Gen.xcworkspace", plist=False)
    assert "not settled" in derived_rows(True)[resolved_only.name]["reason"]
    ledger = scratch.read_local_json(scratch.REPORT.parent / scratch.DERIVED_OWNERS)
    assert ledger[resolved_only.name]["checkout"] == str(generated), ledger
    shutil.rmtree(generated)
    register_thread("gen", settled_at, generated)
    row = derived_rows(True)[resolved_only.name]
    assert row["status"] == "deleted" and row["checkout"] == str(generated), row
    assert resolved_only.name not in scratch.read_local_json(scratch.REPORT.parent / scratch.DERIVED_OWNERS)

    # Build output left inside a settled worktree: ignored folders go, tracked or unignored ones stay.
    def checkout_rows(apply):
        return {Path(r["path"]).name: r for r in scratch.sweep(cfg, apply, sweep_id="cb-fixture")["entries"]
                if r.get("kind") == "checkout-build" and r.get("checkout") == str(package_tree)}
    (package_tree / ".gitignore").write_text(".build/\n")
    (package_tree / ".build" / "checkouts" / "dep" / ".git").mkdir(parents=True)
    (package_tree / ".build" / "checkouts" / "dep" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (package_tree / "DerivedData").mkdir()
    (package_tree / "DerivedData" / "out.o").write_bytes(b"\0" * 4096)
    register_thread("pkg", None, package_tree)
    assert "not settled" in checkout_rows(True)[".build"]["reason"]
    register_thread("pkg", settled_at, package_tree)
    assert "Package.resolved" in checkout_rows(True)[".build"]["reason"]
    shutil.rmtree(package_tree / ".build" / "checkouts")
    with (package_tree / ".build" / "open-fixture").open("w"):
        assert "open files" in checkout_rows(True)[".build"]["reason"]  # This process is not an orphan.
    rows = checkout_rows(False)
    assert rows[".build"]["status"] == "eligible" and "not ignored" in rows["DerivedData"]["reason"], rows
    # A hung helper re-parented to launchd keeps the folder open; the sweep stops it, then removes it.
    subprocess.run(["/bin/sh", "-c", "(cd .build && exec /bin/sleep 120 < open-fixture) &"],
                   cwd=package_tree, check=True)
    deadline = time.time() + 10
    while not scratch.open_holders(package_tree / ".build") and time.time() < deadline:
        time.sleep(0.2)
    holder, = scratch.open_holders(package_tree / ".build")
    assert scratch.process_details(holder)[0] == 1, "fixture process should be orphaned"
    assert "orphaned process" in checkout_rows(False)[".build"]["reason"]
    rows = checkout_rows(True)
    assert rows[".build"]["status"] == "deleted" and not (package_tree / ".build").exists(), rows
    assert (package_tree / "DerivedData").exists() and (package_tree / "Pkg" / "Package.swift").exists()
    try:
        os.kill(holder, 0)
        raise AssertionError("orphaned fixture process is still running")
    except ProcessLookupError:
        pass
    events = [json.loads(line) for path in scratch.AUDIT_DIR.glob("*.jsonl")
              for line in path.read_text().splitlines()]
    stopped, = [e for e in events if e["event"] == "stop_orphans"]
    assert stopped["processes"][0]["pid"] == holder
    assert [e["event"] for e in events if e.get("path") == str(package_tree / ".build")] == [
        "stop_orphans", "delete_intent", "deleted"]

    # The xcodebuild guard and provider hooks.
    for command, blocked in (("cd /x; xcodebuild -list -workspace A.xcworkspace | tail", True),
                             ("xcodebuild -showBuildSettings -scheme S", True),
                             ("FOO=1 xcrun xcodebuild -resolvePackageDependencies", True),
                             ("xcodebuild test -derivedDataPath \"$D\" -scheme S", False),
                             ("xcodebuild \\\n  -scheme S \\\n  -derivedDataPath $D build", False),
                             ("xcodebuild -version", False), ("echo $(xcodebuild -showsdks)", False),
                             ("pgrep -x xcodebuild; sleep 5", False), ("grep -n xcodebuild notes.md", False)):
        assert scratch.bare_xcodebuild(command) == blocked, command
    def guarded(provider, payload):
        out = StringIO()
        with patch.object(sys, "stdin", StringIO(json.dumps(payload))), redirect_stdout(out):
            assert scratch.guard(argparse.Namespace(provider=provider)) == 0
        return json.loads(out.getvalue()) if out.getvalue() else None
    bare = "xcodebuild -list -workspace A.xcworkspace"
    assert guarded("claude", {"tool_input": {"command": bare}, "cwd": str(checkout)})[
        "hookSpecificOutput"]["permissionDecision"] == "deny"
    assert guarded("claude", {"tool_input": {"command": "ls"}, "cwd": str(checkout)}) is None
    assert guarded("claude", {"tool_input": {"command": bare}, "cwd": str(base)}) is None  # Not a T3 worktree.
    agy = {"toolCall": {"name": "run_command", "args": {"CommandLine": bare}}, "cwd": str(checkout)}
    assert guarded("antigravity", agy)["decision"] == "deny"
    agy["toolCall"]["args"]["CommandLine"] = "ls"
    assert guarded("antigravity", agy) == {"decision": "ask"}  # Antigravity treats {} as a denial.
    output = StringIO()
    with redirect_stdout(output):
        assert scratch.artifact_hook(argparse.Namespace(
            provider="antigravity", cwd=str(checkout), session="agy-fixture")) == 0
    step, = json.loads(output.getvalue())["injectSteps"]
    assert "env.sh" in step["systemMessage"]["systemMessage"]
    for folder in (".claude", ".codex", ".gemini/config"):
        (base / folder).mkdir(parents=True, exist_ok=True)
    scratch.T3_ANTIGRAVITY_PROFILES = base / "t3-antigravity"
    (scratch.T3_ANTIGRAVITY_PROFILES / "instance" / "config").mkdir(parents=True)
    changes = dict(scratch.hook_updates())
    claude = changes[base / ".claude/settings.json"]["hooks"]
    assert claude["PreToolUse"][0]["matcher"] == "Bash" and "guard claude" in json.dumps(claude["PreToolUse"])
    assert "guard codex" in json.dumps(changes[base / ".codex/hooks.json"]["hooks"]["PreToolUse"])
    for path in (base / ".gemini/config/hooks.json", scratch.T3_ANTIGRAVITY_PROFILES / "instance/config/hooks.json"):
        hooks = changes[path]
        assert "artifact-hook antigravity" in json.dumps(hooks[scratch.HOOK_NAMES["artifacts"]]["SessionStart"])
        assert hooks[scratch.HOOK_NAMES["guard"]]["PreToolUse"][0]["matcher"] == "run_command"
    for path, data in changes.items():
        scratch.write_json(path, data)
    assert scratch.hook_updates() == []  # Installing again changes nothing.

    # Storage report: sizes are recorded per sweep and compared with a fresh measurement.
    grown = base / "grown"
    grown.mkdir()
    (grown / "a.bin").write_bytes(b"\0" * 1024 * 1024)
    cfg["report_paths"] = [str(grown), str(base / "missing")]
    scratch.ensure_directory(scratch.REPORT.parent)
    scratch.record_sizes(cfg)
    (grown / "b.bin").write_bytes(b"\0" * 4 * 1024 * 1024)
    result = scratch.storage_report(cfg, None)
    rows = {row["path"]: row for row in result["paths"]}
    assert result["baseline_at"] and rows[str(grown)]["change_kib"] >= 4000, rows
    assert rows[str(base / "missing")]["now_kib"] is None
    assert cfg["build_artifacts_root"] in rows
    assert scratch.storage_report(cfg, scratch.parse_since("2026-01-01T00:00:00+00:00"))["baseline_at"]
    future = datetime.now(timezone.utc).replace(year=2999)
    assert scratch.storage_report(cfg, future)["baseline_at"] is None
    assert scratch.parse_since("2h") < datetime.now(timezone.utc)
    conn.close()
    os.chdir(base.parent)
print("PASS: opted-in allocation, settlement, shared checkout, queued run, detached and bound sessions, lock, open file, T3 unavailable, preview, and scoped removal")
print("PASS: hook-allocated build cache, shared owners, evidence, aliases, audit and resumed threads")
print("PASS: project stray files: unsafe patterns, owner attribution, orphans, settlement, grace, open file, audit")
print("PASS: evidence retention: age, settlement, open file, disabled, preview, audit; storage report history")
print("PASS: Xcode DerivedData: info.plist and hash attribution, outside and mismatched folders, orphans, settlement, open file, audit")
print("PASS: ignored generated workspaces, owners kept after worktree removal, in-worktree build output, orphaned holders")
print("PASS: xcodebuild guard, Antigravity startup hook, and idempotent hook install for every provider")
