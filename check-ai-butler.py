#!/usr/bin/python3
"""Exercise the CLI and its launchd operations without installing a real service."""

from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import importlib.util
from importlib.machinery import SourceFileLoader
import io
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_loader("butler", SourceFileLoader(
    "butler", str(Path(__file__).with_name("ai-butler"))))
butler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(butler)

with tempfile.TemporaryDirectory(dir=sys.argv[1]) as temporary:
    base = Path(temporary).resolve()
    butler.HOME = base
    butler.xcode_derived_data = lambda: base / "DerivedData"  # Never read the real Xcode folder.
    butler.ROOT = base / "scratch"
    butler.CONFIG = base / "config" / "config.json"
    butler.PLIST = base / "agents" / (butler.LABEL + ".plist")
    butler.REPORT = base / "reports" / "latest.json"
    butler.AUDIT_DIR = butler.REPORT.parent / "audit"
    live = None
    fail_interval = None
    changes = []

    def launchctl(*args, check=True):
        global live
        if args[0] == "print":
            return subprocess.CompletedProcess(args, int(live is None), "",
                                               "Could not find service" if live is None else "")
        changes.append(args[0])
        if args[0] == "bootout":
            assert live is not None
            live = None
        elif args[0] == "bootstrap":
            assert live is None and Path(args[2]) == butler.PLIST
            incoming = plistlib.loads(butler.PLIST.read_bytes())
            if incoming["StartInterval"] == fail_interval:
                raise subprocess.CalledProcessError(1, args, stderr="fixture rejected interval")
            live = incoming
        else:
            raise AssertionError(args)

    def cli(*args, code=0):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["ai-butler", *map(str, args)]), \
                redirect_stdout(output), redirect_stderr(errors):
            assert butler.main() == code, (args, output.getvalue(), errors.getvalue())
        return output.getvalue()

    with patch.object(butler, "launchctl", launchctl):
        assert json.loads(cli("list", "--json"))["interval_minutes"] == 60
        assert not butler.CONFIG.exists()
        cli("_sweep", code=1)
        assert "error" in json.loads(cli("status"))["last_sweep"]
        assert not butler.CONFIG.exists()
        cli("setInterval", 8)
        cli("setSettlementGracePeriod", 2)
        assert json.loads(cli("list", "--json"))["log_retention_days"] == 7
        cli("setLogRetention", 3)
        cli("setLogRetention", 0, code=1)
        cfg = butler.settings()
        cfg["scratch_root"] = str(base / "custom-scratch")
        butler.write_json(butler.CONFIG, cfg)
        assert json.loads(cli("list", "--json"))["deletion_boundary"] == cfg["scratch_root"]
        cfg["scratch_root"] = str(base.parent)
        butler.write_json(butler.CONFIG, cfg)
        cli("list", "--json", code=1)
        cfg["scratch_root"] = str(base / "scratch")
        butler.write_json(butler.CONFIG, cfg)
        cli("list", "--json")
        assert not changes
        pool = butler.ROOT / "checks"
        cli("add", pool)
        assert pool.is_dir()
        nonempty = butler.ROOT / "existing"
        nonempty.mkdir()
        (nonempty / "keep").touch()
        cli("add", nonempty, code=1)
        cli("add", base / "outside", code=1)
        alias = butler.ROOT / "alias"
        alias.symlink_to(nonempty, target_is_directory=True)
        cli("add", alias, code=1)
        (pool / "keep").touch()
        cli("remove", pool)
        assert (pool / "keep").exists()
        cli("add", pool, code=1)
        cli("remove", butler.ROOT / "tmp")
        assert json.loads(cli("list", "--json"))["pools"] == []
        assert json.loads(cli("preview"))["entries"] == []
        cli("install")
        cli("install")
        assert live["StartInterval"] == 480 and changes == ["bootstrap"]
        assert json.loads(cli("status"))["loaded"] is True
        cli("setInterval", 0)
        assert live["StartInterval"] == 3600
        assert changes[-2:] == ["bootout", "bootstrap"]
        fail_interval = 180
        cli("setInterval", 3, code=1)
        assert live["StartInterval"] == 3600 and butler.settings()["interval_minutes"] == 60
        today = datetime.now(timezone.utc).date()
        recent = butler.AUDIT_DIR / f"{today - timedelta(days=2)}.jsonl"
        expired = butler.AUDIT_DIR / f"{today - timedelta(days=3)}.jsonl"
        unrelated = butler.AUDIT_DIR / "unrelated.jsonl"
        alias_log = butler.AUDIT_DIR / "2000-01-01.jsonl"
        for path in (recent, expired, unrelated):
            path.write_text("{}\n")
        alias_log.symlink_to(unrelated)
        cli("_sweep")
        assert not expired.exists() and recent.exists() and unrelated.exists() and alias_log.is_symlink()
        assert json.loads(cli("status"))["last_sweep"]["entries"] == []
        events = [json.loads(line) for line in (butler.AUDIT_DIR / f"{today}.jsonl").read_text().splitlines()]
        assert [entry["event"] for entry in events] == ["sweep_finished", "sweep_started", "sweep_finished"]
        assert events[-2]["sweep_id"] == events[-1]["sweep_id"]
        cli("uninstall")
        cli("uninstall")
        assert live is None and not butler.PLIST.exists()
        assert (pool / "keep").exists() and butler.CONFIG.exists()
        other_hook = {"type": "command", "command": "keep-existing-hook"}
        for folder in (".codex", ".claude", ".cursor"):
            (base / folder).mkdir()
        codex = base / ".codex/hooks.json"
        claude = base / ".claude/settings.json"
        cursor = base / ".cursor/hooks.json"
        legacy_hook = {"type": "command", "timeout": 5, "command":
                       f"/usr/bin/python3 {base}/.agents/hooks/artifact-storage.py codex"}
        butler.write_json(codex, {"hooks": {"SessionStart": [{"hooks": [legacy_hook, other_hook]}]}})
        butler.write_json(claude, {"permissions": {"allow": ["fixture"]}})
        butler.write_json(cursor, {"version": 1, "hooks": {"sessionStart": [{"command": "keep"}]}})
        cli("install", "--hooks", "--artifact-root", base / "artifacts")
        assert not butler.hook_updates()
        cli("install", "--hooks")
        assert butler.settings()["build_artifacts_root"] == str(base / "artifacts")
        hooks = butler.read_local_json(codex)["hooks"]["SessionStart"][0]["hooks"]
        assert len(hooks) == 2 and hooks[1] == other_hook
        assert hooks[0]["command"].endswith("artifact-hook codex")
        assert butler.read_local_json(claude)["permissions"] == {"allow": ["fixture"]}
        assert len(butler.read_local_json(cursor)["hooks"]["sessionStart"]) == 2
        cli("uninstall")
        butler.PLIST.write_bytes(plistlib.dumps({"Label": "other"}))
        cli("uninstall", code=1)
        assert butler.PLIST.exists()
print("PASS: CLI pool boundaries, defaults, timings, service lifecycle, rollback, and reports")
