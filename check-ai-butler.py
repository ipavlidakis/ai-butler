#!/usr/bin/python3
"""Exercise the CLI and its launchd operations without installing a real service."""

from contextlib import redirect_stdout, redirect_stderr
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
    butler.ROOT = base / "scratch"
    butler.CONFIG = base / "config" / "config.json"
    butler.PLIST = base / "agents" / (butler.LABEL + ".plist")
    butler.REPORT = base / "reports" / "latest.json"
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
        cli("_sweep")
        assert json.loads(cli("status"))["last_sweep"]["entries"] == []
        cli("uninstall")
        cli("uninstall")
        assert live is None and not butler.PLIST.exists()
        assert (pool / "keep").exists() and butler.CONFIG.exists()
        butler.PLIST.write_bytes(plistlib.dumps({"Label": "other"}))
        cli("uninstall", code=1)
        assert butler.PLIST.exists()
print("PASS: CLI pool boundaries, defaults, timings, service lifecycle, rollback, and reports")
