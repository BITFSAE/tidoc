"""Run the shipped update helpers against isolated fake clients.

These tests execute the real macOS shell and Windows PowerShell helpers instead of
matching their source. The fake clients report health and data compatibility, so the
tests cover directory swaps, process waits, probe deadlines, and the recovery window.
Every path contains Chinese characters and spaces; nothing touches real user data.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tidoc.services.updater import MACOS_INSTALLER_SCRIPT, WINDOWS_INSTALLER_SCRIPT

VERSION = "0.1.44-beta.3"


def _lines(path: Path) -> list[str]:
    try:
        return path.read_text("utf-8").splitlines()
    except (FileNotFoundError, UnicodeError):
        return []


def _wait_for(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _contains(path: Path, needle: str) -> bool:
    return any(needle in line for line in _lines(path))


MACOS_FAKE_CLIENT = r'''#!/bin/sh
log="${TIDOC_FAKE_LOG:-}"
note() { [ -n "$log" ] && printf '%s\n' "$1" >> "$log"; }
health=""
while [ $# -gt 0 ]; do
  case "$1" in
    --check-data-compatibility)
      note probe
      if [ "${TIDOC_FAKE_PROBE_SLEEP:-0}" -gt 0 ]; then
        exec sleep "${TIDOC_FAKE_PROBE_SLEEP}"
      fi
      exit "${TIDOC_FAKE_PROBE_EXIT:-0}"
      ;;
    --update-health-file)
      health="$2"
      shift 2
      ;;
    --update-recovery)
      note recovery
      exit 0
      ;;
    *) shift ;;
  esac
done
if [ -n "$health" ]; then
  note start
  if [ "${TIDOC_FAKE_HEALTH:-write}" = "write" ]; then
    printf '{"pid": %s, "version": "%s"}\n' "$$" "$TIDOC_FAKE_VERSION" > "$health.$$"
    mv "$health.$$" "$health"
    sleep "${TIDOC_FAKE_LINGER:-0.3}"
    exit 0
  fi
  if [ "${TIDOC_FAKE_HEALTH:-}" = "hang" ]; then
    exec sleep 30
  fi
  exit 1
fi
note launch
exit 0
'''


def _run_macos_helper(
    tmp_path: Path,
    *,
    probe_exit: int = 0,
    health_mode: str = "write",
    probe_sleep: int = 0,
    probe_timeout: int = 2,
    health_ticks: int = 8,
    drop_old_executable: bool = False,
) -> SimpleNamespace:
    root = tmp_path / "安装 目录"
    app = root / "Tidoc 旧版.app"
    staged = root / "Tidoc 新版.app"
    work = root / "更新 工作区"
    root.mkdir()
    work.mkdir()
    for bundle, marker in ((app, "OLD"), (staged, "NEW")):
        executable = bundle / "Contents" / "MacOS" / "tidoc"
        executable.parent.mkdir(parents=True)
        executable.write_text(MACOS_FAKE_CLIENT, "utf-8")
        executable.chmod(0o755)
        (bundle / marker).write_text(marker, "utf-8")
    if drop_old_executable:
        (app / "Contents" / "MacOS" / "tidoc").unlink()

    stub_bin = tmp_path / "stub bin"
    stub_bin.mkdir()
    open_log = root / "open.log"
    stub_open = stub_bin / "open"
    stub_open.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$TIDOC_OPEN_LOG"\n',
        "utf-8",
    )
    stub_open.chmod(0o755)

    fake_log = root / "fake.log"
    health = work / "health.json"
    script = tmp_path / "install-helper.sh"
    script.write_text(MACOS_INSTALLER_SCRIPT, "utf-8")
    script.chmod(0o700)

    env = os.environ.copy()
    env.update({
        "PATH": f"{stub_bin}:{env.get('PATH', '')}",
        "TIDOC_OPEN_LOG": str(open_log),
        "TIDOC_FAKE_LOG": str(fake_log),
        "TIDOC_FAKE_VERSION": VERSION,
        "TIDOC_FAKE_PROBE_EXIT": str(probe_exit),
        "TIDOC_FAKE_PROBE_SLEEP": str(probe_sleep),
        "TIDOC_FAKE_HEALTH": health_mode,
        "TIDOC_FAKE_LINGER": "0.3",
    })
    started = time.monotonic()
    result = subprocess.run(
        [
            "/bin/sh", str(script), str(app), str(staged), str(work), VERSION,
            str(health), "999999", str(probe_timeout), str(health_ticks),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(root),
    )
    return SimpleNamespace(
        result=result,
        root=root,
        app=app,
        staged=staged,
        work=work,
        fake_log=fake_log,
        open_log=open_log,
        elapsed=time.monotonic() - started,
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="runs the macOS shell helper")
def test_macos_healthy_update_replaces_app_and_cleans_backup(tmp_path):
    run = _run_macos_helper(tmp_path)

    assert run.result.returncode == 0, run.result.stderr
    assert (run.app / "NEW").is_file()
    assert not (run.app / "OLD").exists()
    assert not list(run.root.glob("*.old-*"))
    assert not list(run.root.glob("*.failed-*"))
    assert not run.staged.exists()
    assert not run.work.exists()
    assert _lines(run.open_log) == []
    assert _contains(run.fake_log, "start")


@pytest.mark.skipif(sys.platform != "darwin", reason="runs the macOS shell helper")
def test_macos_failure_before_migration_restores_old_app(tmp_path):
    run = _run_macos_helper(tmp_path, probe_exit=0, health_mode="exit")

    assert run.result.returncode == 23, run.result.stderr
    assert (run.app / "OLD").is_file()
    assert not (run.app / "NEW").exists()
    assert not list(run.root.glob("*.old-*"))
    opened = _lines(run.open_log)
    assert len(opened) == 1
    assert opened[0].startswith(f"-n {run.app}")
    assert "--update-recovery" not in opened[0]


@pytest.mark.skipif(sys.platform != "darwin", reason="runs the macOS shell helper")
def test_macos_failure_after_migration_keeps_both_apps_and_opens_recovery(tmp_path):
    run = _run_macos_helper(tmp_path, probe_exit=1, health_mode="exit")

    assert run.result.returncode == 24, run.result.stderr
    assert (run.app / "NEW").is_file()
    backups = list(run.root.glob("*.old-*"))
    assert len(backups) == 1
    assert (backups[0] / "OLD").is_file()
    opened = _lines(run.open_log)
    assert len(opened) == 1
    assert "--update-recovery" in opened[0]


@pytest.mark.skipif(sys.platform != "darwin", reason="runs the macOS shell helper")
def test_macos_missing_probe_keeps_new_app_instead_of_restoring(tmp_path):
    run = _run_macos_helper(
        tmp_path, probe_exit=0, health_mode="exit", drop_old_executable=True,
    )

    assert run.result.returncode == 24, run.result.stderr
    assert (run.app / "NEW").is_file()
    assert len(list(run.root.glob("*.old-*"))) == 1
    assert _wait_for(lambda: _contains(run.open_log, "recovery"))


@pytest.mark.skipif(sys.platform != "darwin", reason="runs the macOS shell helper")
def test_macos_probe_deadline_keeps_new_app_instead_of_restoring(tmp_path):
    run = _run_macos_helper(
        tmp_path, probe_exit=0, health_mode="exit", probe_sleep=30, probe_timeout=1,
    )

    assert run.result.returncode == 24, run.result.stderr
    assert run.elapsed < 10
    assert (run.app / "NEW").is_file()
    assert _wait_for(lambda: _contains(run.open_log, "recovery"))


@pytest.mark.skipif(sys.platform != "darwin", reason="runs the macOS shell helper")
def test_macos_unhealthy_deadline_kills_new_app_then_probes(tmp_path):
    run = _run_macos_helper(
        tmp_path, probe_exit=1, health_mode="hang", health_ticks=4,
    )

    assert run.result.returncode == 24, run.result.stderr
    assert run.elapsed >= 1
    assert (run.app / "NEW").is_file()
    assert _wait_for(lambda: _contains(run.open_log, "recovery"))


WINDOWS_FAKE_CLIENT = r'''
using System;
using System.Diagnostics;
using System.IO;
using System.Threading;

public static class FakeTidoc
{
    private static bool Has(string[] args, string value)
    {
        foreach (var item in args) if (item == value) return true;
        return false;
    }

    private static void Note(string line)
    {
        var path = Environment.GetEnvironmentVariable("TIDOC_FAKE_LOG");
        if (!string.IsNullOrEmpty(path)) File.AppendAllText(path, line + Environment.NewLine);
    }

    public static int Main(string[] args)
    {
        if (Has(args, "--check-data-compatibility"))
        {
            Note("probe");
            var sleep = Environment.GetEnvironmentVariable("TIDOC_FAKE_PROBE_SLEEP_MS");
            if (!string.IsNullOrEmpty(sleep)) Thread.Sleep(int.Parse(sleep));
            var code = Environment.GetEnvironmentVariable("TIDOC_FAKE_PROBE_EXIT");
            return string.IsNullOrEmpty(code) ? 0 : int.Parse(code);
        }
        if (Has(args, "--update-recovery"))
        {
            Note("recovery");
            return 0;
        }
        var health = -1;
        for (var i = 0; i < args.Length - 1; i++)
        {
            if (args[i] == "--update-health-file") health = i + 1;
        }
        if (health >= 0)
        {
            Note("start");
            var mode = Environment.GetEnvironmentVariable("TIDOC_FAKE_HEALTH");
            if (mode == "exit") return 1;
            if (mode == "hang") { Thread.Sleep(60000); return 1; }
            var version = Environment.GetEnvironmentVariable("TIDOC_FAKE_VERSION") ?? "0.0.0";
            var pending = args[health] + "." + Process.GetCurrentProcess().Id + ".tmp";
            File.WriteAllText(pending,
                "{\"pid\": " + Process.GetCurrentProcess().Id +
                ", \"version\": \"" + version + "\"}");
            File.Move(pending, args[health]);
            Thread.Sleep(400);
            return 0;
        }
        Note("launch");
        return 0;
    }
}
'''

WINDOWS_COMPILE_SCRIPT = r'''
param(
    [Parameter(Mandatory=$true)][string]$Source,
    [Parameter(Mandatory=$true)][string]$Output
)
$ErrorActionPreference = "Stop"
$code = Get-Content -LiteralPath $Source -Raw
Add-Type -TypeDefinition $code -OutputAssembly $Output -OutputType WindowsApplication
'''


@pytest.fixture(scope="module")
def windows_fake_exe(tmp_path_factory):
    if sys.platform != "win32":
        pytest.skip("runs the Windows PowerShell helper")
    root = tmp_path_factory.mktemp("fake-client")
    source = root / "FakeTidoc.cs"
    source.write_text(WINDOWS_FAKE_CLIENT, "utf-8")
    build = root / "build.ps1"
    build.write_text(WINDOWS_COMPILE_SCRIPT, "utf-8")
    executable = root / "tidoc.exe"
    completed = subprocess.run(
        [
            "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(build), "-Source", str(source), "-Output", str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert executable.is_file()
    return executable


def _run_windows_helper(
    tmp_path: Path,
    fake_exe: Path,
    *,
    probe_exit: int = 0,
    health_mode: str = "write",
    probe_sleep_ms: int = 0,
    health_timeout: int = 2,
    probe_timeout_ms: int = 2000,
    drop_old_executable: bool = False,
) -> SimpleNamespace:
    root = tmp_path / "安装 目录"
    app = root / "Tidoc 旧版"
    staged = root / "Tidoc 新版"
    work = root / "更新 工作区"
    root.mkdir()
    app.mkdir()
    staged.mkdir()
    work.mkdir()
    shutil.copy2(fake_exe, app / "tidoc.exe")
    (app / "OLD").write_text("old", "utf-8")
    (app / "unins000.exe").write_text("uninstaller", "utf-8")
    shutil.copy2(fake_exe, staged / "tidoc.exe")
    (staged / "NEW").write_text("new", "utf-8")
    if drop_old_executable:
        (app / "tidoc.exe").unlink()

    fake_log = root / "fake.log"
    health = work / "health.json"
    script = root / "install-helper.ps1"
    script.write_text(WINDOWS_INSTALLER_SCRIPT, "utf-8")

    env = os.environ.copy()
    env.update({
        "TIDOC_FAKE_LOG": str(fake_log),
        "TIDOC_FAKE_VERSION": VERSION,
        "TIDOC_FAKE_PROBE_EXIT": str(probe_exit),
        "TIDOC_FAKE_PROBE_SLEEP_MS": str(probe_sleep_ms),
        "TIDOC_FAKE_HEALTH": health_mode,
    })
    started = time.monotonic()
    result = subprocess.run(
        [
            "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(script),
            "-AppDir", str(app), "-StagedDir", str(staged), "-WorkDir", str(work),
            "-ExpectedVersion", VERSION, "-HealthFile", str(health), "-OldPid", "0",
            "-HealthTimeoutSeconds", str(health_timeout),
            "-ProbeTimeoutMs", str(probe_timeout_ms),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
        cwd=str(root),
    )
    return SimpleNamespace(
        result=result,
        root=root,
        app=app,
        staged=staged,
        work=work,
        fake_log=fake_log,
        elapsed=time.monotonic() - started,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="runs the Windows PowerShell helper")
def test_windows_healthy_update_replaces_app_and_cleans_backup(
    tmp_path, windows_fake_exe,
):
    run = _run_windows_helper(tmp_path, windows_fake_exe)

    assert run.result.returncode == 0, run.result.stdout + run.result.stderr
    time.sleep(0.8)  # Let the fake replacement exit and release its executable file.
    assert (run.app / "NEW").is_file()
    assert not (run.app / "OLD").exists()
    assert (run.app / "unins000.exe").is_file()
    assert not list(run.root.glob("*.old-*"))
    assert not list(run.root.glob("*.failed-*"))
    assert not run.staged.exists()
    assert not run.work.exists()
    assert _contains(run.fake_log, "start")


@pytest.mark.skipif(sys.platform != "win32", reason="runs the Windows PowerShell helper")
def test_windows_failure_before_migration_restores_old_app(tmp_path, windows_fake_exe):
    run = _run_windows_helper(
        tmp_path, windows_fake_exe, probe_exit=0, health_mode="exit",
    )

    assert run.result.returncode == 23, run.result.stdout + run.result.stderr
    assert (run.app / "OLD").is_file()
    assert not (run.app / "NEW").exists()
    assert not list(run.root.glob("*.old-*"))
    assert _wait_for(lambda: _contains(run.fake_log, "launch"))


@pytest.mark.skipif(sys.platform != "win32", reason="runs the Windows PowerShell helper")
def test_windows_failure_after_migration_keeps_both_apps_and_opens_recovery(
    tmp_path, windows_fake_exe,
):
    run = _run_windows_helper(
        tmp_path, windows_fake_exe, probe_exit=1, health_mode="exit",
    )

    assert run.result.returncode == 24, run.result.stdout + run.result.stderr
    assert (run.app / "NEW").is_file()
    backups = list(run.root.glob("*.old-*"))
    assert len(backups) == 1
    assert (backups[0] / "OLD").is_file()
    assert _wait_for(lambda: _contains(run.fake_log, "recovery"))


@pytest.mark.skipif(sys.platform != "win32", reason="runs the Windows PowerShell helper")
def test_windows_missing_probe_keeps_new_app_instead_of_restoring(
    tmp_path, windows_fake_exe,
):
    run = _run_windows_helper(
        tmp_path, windows_fake_exe, probe_exit=0, health_mode="exit",
        drop_old_executable=True,
    )

    assert run.result.returncode == 24, run.result.stdout + run.result.stderr
    assert (run.app / "NEW").is_file()
    assert len(list(run.root.glob("*.old-*"))) == 1
    assert _wait_for(lambda: _contains(run.fake_log, "recovery"))


@pytest.mark.skipif(sys.platform != "win32", reason="runs the Windows PowerShell helper")
def test_windows_probe_deadline_keeps_new_app_instead_of_restoring(
    tmp_path, windows_fake_exe,
):
    run = _run_windows_helper(
        tmp_path,
        windows_fake_exe,
        probe_exit=0,
        health_mode="exit",
        probe_sleep_ms=30000,
        probe_timeout_ms=1500,
    )

    assert run.result.returncode == 24, run.result.stdout + run.result.stderr
    assert run.elapsed < 20
    assert (run.app / "NEW").is_file()
    assert _wait_for(lambda: _contains(run.fake_log, "recovery"))


@pytest.mark.skipif(sys.platform != "win32", reason="runs the Windows PowerShell helper")
def test_windows_unhealthy_deadline_kills_new_app_then_probes(
    tmp_path, windows_fake_exe,
):
    run = _run_windows_helper(
        tmp_path, windows_fake_exe, probe_exit=1, health_mode="hang", health_timeout=2,
    )

    assert run.result.returncode == 24, run.result.stdout + run.result.stderr
    assert run.elapsed >= 2
    assert (run.app / "NEW").is_file()
    assert _wait_for(lambda: _contains(run.fake_log, "recovery"))
