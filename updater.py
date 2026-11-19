"""Keeps Clip Agent current from GitHub. Compares manifest.json (file -> sha256) with the local copy and
downloads only files that changed. Safe to run offline: it just reports an error and changes nothing."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Some Python installs (python.org on macOS without "Install Certificates.command") have no system CA
# bundle. Pass this explicitly to urlopen() below rather than patching ssl's default context: urlopen()
# builds and caches a global opener on its first-ever call using whatever context was default at that
# moment, so a later patch (e.g. from launch.pyw's own startup check, which runs before this module is
# even imported) would arrive too late.
try:
    import certifi, ssl
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CTX = None

HERE = Path(__file__).resolve().parent
REPO = "jainaarav257-netizen/clip-agent-mac"
BRANCH = "main"
RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/"
STATUS = HERE / ".update_status.json"


def _get(path: str, timeout: float = 8.0) -> bytes:
    req = urllib.request.Request(RAW + path + f"?t={int(time.time())}", headers={"Cache-Control": "no-cache",
                                                                                   "User-Agent": "clip-agent"})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.read()


def _sha(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def _python_console() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return str(exe)


def update(log=print) -> dict:
    res = {"checked": time.time(), "updated": False, "files": [], "error": None}
    try:
        remote = json.loads(_get("manifest.json"))
        local_v = None
        try:
            local_v = json.loads((HERE / "manifest.json").read_text()).get("version")
        except Exception:
            pass
        changed = []
        for rel, sha in remote.get("files", {}).items():
            if ".." in rel or rel.startswith(("/", "\\")):
                continue
            if _sha(HERE / rel) != sha:
                changed.append((rel, sha))
        for rel, sha in changed:
            data = _get(rel, timeout=30)
            if hashlib.sha256(data).hexdigest() != sha:
                raise RuntimeError(f"Downloaded {rel} didn't match (GitHub may still be updating). Try again shortly.")
            dest = HERE / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".new")
            tmp.write_bytes(data)
            os.replace(tmp, dest)
            res["files"].append(rel)
            log(f"Updated {rel}")
        (HERE / "manifest.json").write_text(json.dumps(remote, indent=2))
        if "requirements.txt" in res["files"]:
            log("Installing new requirements...")
            flags = 0x08000000 if sys.platform.startswith("win") else 0  # no console window
            subprocess.run([_python_console(), "-m", "pip", "install", "-q", "--timeout", "120", "--retries", "10",
                            "-r", str(HERE / "requirements.txt")],
                           creationflags=flags, check=False)
        res["updated"] = bool(res["files"])
        res["version"] = remote.get("version")
        res["previous"] = local_v
    except Exception as e:
        res["error"] = str(e)[:300]
    try:
        STATUS.write_text(json.dumps(res))
    except Exception:
        pass
    return res


if __name__ == "__main__":
    import sys as _sys
    _res = update()
    print(_res)
    if _res.get("error"):
        _sys.exit(1)
