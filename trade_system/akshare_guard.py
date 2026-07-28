"""Daily AKShare freshness gate.

AKShare is an adapter over changing public websites.  A market-data run must
upgrade it once before importing it, then keep that version fixed for the
rest of the process.  The marker is local runtime state and is intentionally
not part of the database or source control.
"""

from __future__ import annotations

from datetime import date
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "trade_system" / ".stock_cache"
MARKER = STATE_DIR / "akshare_refresh.json"
LOCK = STATE_DIR / "akshare_refresh.lock"


class AkshareRefreshError(RuntimeError):
    pass


def _version() -> str:
    try:
        return importlib.metadata.version("akshare")
    except importlib.metadata.PackageNotFoundError:
        return ""


def _acquire_lock(timeout: float = 180.0) -> bool:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            LOCK.mkdir()
            return True
        except FileExistsError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)


def ensure_akshare_current(*, force: bool = False, timeout: int = 180) -> dict[str, str]:
    """Upgrade AKShare once for the local trading date and return its version."""
    today = date.today().isoformat()
    if not force and MARKER.exists():
        try:
            state = json.loads(MARKER.read_text(encoding="utf-8"))
            if state.get("date") == today and state.get("status") == "success" and state.get("version"):
                return {"date": today, "version": str(state["version"]), "status": "cached"}
        except (OSError, ValueError, TypeError):
            pass
    if not _acquire_lock():
        raise AkshareRefreshError("timed out waiting for the daily AKShare upgrade lock")
    try:
        # Another process may have completed the upgrade while we waited.
        if not force and MARKER.exists():
            try:
                state = json.loads(MARKER.read_text(encoding="utf-8"))
                if state.get("date") == today and state.get("status") == "success" and state.get("version"):
                    return {"date": today, "version": str(state["version"]), "status": "cached"}
            except (OSError, ValueError, TypeError):
                pass
        command = [
            sys.executable, "-m", "pip", "install", "akshare", "--upgrade",
            "-i", "https://pypi.org/simple", "--disable-pip-version-check",
        ]
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=max(30, int(timeout)), check=False,
        )
        version = _version()
        state = {
            "date": today, "version": version,
            "status": "success" if completed.returncode == 0 and version else "failed",
            "returncode": str(completed.returncode),
        }
        MARKER.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        if state["status"] != "success":
            raise AkshareRefreshError(f"AKShare daily upgrade failed (returncode={completed.returncode})")
        return {"date": today, "version": version, "status": "upgraded"}
    finally:
        shutil.rmtree(LOCK, ignore_errors=True)

