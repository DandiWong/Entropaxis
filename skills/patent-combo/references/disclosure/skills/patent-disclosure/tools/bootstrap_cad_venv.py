# -*- coding: utf-8 -*-
"""Create the isolated ``tools/cad-env`` from exact dependency pins.

Installation uses only ``https://pypi.org/simple`` with isolated pip settings.
Custom indexes and environment-provided package sources are intentionally rejected.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_SHARED = Path(__file__).resolve().parent
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

from cad_venv import (
    VENV_DIR,
    VENV_NAME,
    find_supported_python,
    isolated_env,
    status,
    venv_python,
    version_supported,
    write_meta,
)

REQ_FILE = _SHARED / "requirements-step.txt"

PYPI_INDEX = "https://pypi.org/simple"


def _remove_stale_venv() -> None:
    if VENV_DIR.is_symlink():
        raise RuntimeError(f"refusing to delete symlinked CAD environment: {VENV_DIR}")
    target = VENV_DIR.resolve()
    tools_dir = _SHARED.resolve()
    if target.parent != tools_dir or target.name != VENV_NAME:
        raise RuntimeError(f"refusing to delete path outside tools directory: {target}")
    shutil.rmtree(VENV_DIR)


def _run(cmd: list[str], *, env: dict[str, str] | None = None, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        env=env or isolated_env(),
        timeout=timeout,
        text=True,
        capture_output=True,
    )


def _ensure_pip(py: Path) -> None:
    probe = _run([str(py), "-m", "pip", "--version"], timeout=30)
    if probe.returncode == 0:
        return
    print("CAD_VENV: ensurepip", file=sys.stderr, flush=True)
    ep = _run([str(py), "-m", "ensurepip", "--upgrade"], timeout=120)
    if ep.returncode != 0:
        raise RuntimeError(
            "cad-env has no pip and ensurepip failed: "
            + (ep.stderr or ep.stdout or str(ep.returncode))
        )


def _create_venv(py_exe: str) -> Path:
    VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
    existing = venv_python(VENV_DIR)
    if existing.is_file():
        from cad_venv import python_version

        ver = python_version(existing)
        if version_supported(ver):
            return existing
        print("CAD_VENV: recreate_unsupported_python", file=sys.stderr, flush=True)
        _remove_stale_venv()
    print(f"CAD_VENV: creating {VENV_DIR} with {py_exe}", file=sys.stderr, flush=True)
    created = _run([py_exe, "-m", "venv", str(VENV_DIR)], timeout=120)
    if created.returncode != 0:
        raise RuntimeError("venv create failed: " + (created.stderr or created.stdout or ""))
    py = venv_python(VENV_DIR)
    if not py.is_file():
        raise RuntimeError(f"venv python missing: {py}")
    return py


def _pip_install(py: Path) -> str:
    if not REQ_FILE.is_file():
        raise RuntimeError(f"missing {REQ_FILE}")
    env = isolated_env()
    cmd = [
        str(py),
        "-m",
        "pip",
        "--isolated",
        "install",
        "--disable-pip-version-check",
        "--only-binary=:all:",
        "--index-url",
        PYPI_INDEX,
        "-r",
        str(REQ_FILE),
    ]
    print(f"PIP_INDEX: {PYPI_INDEX}", file=sys.stderr, flush=True)
    proc = _run(cmd, env=env, timeout=900)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit={proc.returncode}")[-2000:]
        raise RuntimeError("locked dependency install failed: " + detail)
    return PYPI_INDEX


def bootstrap() -> dict:
    st = status()
    if st["ok"]:
        print(
            "CAD_VENV: already_ready skip_install=true",
            file=sys.stderr,
            flush=True,
        )
        return st

    py_exe, ver = find_supported_python()
    if not py_exe:
        raise RuntimeError(
            "CadQuery needs Python 3.10-3.12. Current interpreter is "
            f"{sys.version.split()[0]}. Install 3.10, 3.11, or 3.12 and retry."
        )
    print(
        f"CAD_VENV: using_python={py_exe} version={ver[0]}.{ver[1]}.{ver[2]}",
        file=sys.stderr,
        flush=True,
    )
    venv_py = _create_venv(py_exe)
    _ensure_pip(venv_py)
    used = _pip_install(venv_py)
    write_meta(
        {
            "venv_name": VENV_NAME,
            "host_python": sys.executable,
            "venv_python": str(venv_py),
            "base_python": py_exe,
            "pip_index": used,
            "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        }
    )
    st = status()
    if not st["ok"]:
        raise RuntimeError("bootstrap finished but cadquery still missing: " + json.dumps(st, ensure_ascii=False))
    return st


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Create or reuse tools/cad-env from exact pins on pypi.org")
    p.parse_args(argv)
    try:
        st = bootstrap()
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(st, ensure_ascii=False, indent=2))
    print(
        f"CAD_VENV: ok={'true' if st['ok'] else 'false'} skip_install="
        f"{'true' if st.get('skip_install') else 'false'} reason={st.get('reason') or ''}",
        file=sys.stderr,
        flush=True,
    )
    return 0 if st["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
