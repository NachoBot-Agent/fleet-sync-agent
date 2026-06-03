"""Bootstrap entry point.

Pulled fresh from the public repo on every launch by the user's
personalized .cmd. Decides whether the local install is up to date,
downloads + installs the latest release if not, ensures pip deps are
present, writes the agent config from env vars, and launches the
agent.

The .cmd file the user runs sets these env vars before invoking us:

    FLEET_CIPHER     — the 64-hex account cipher (required)
    FLEET_PILOT      — pilot display name (cosmetic, optional)
    FLEET_API_URL    — server API root (defaults to starfleet.sc)

Stdlib only — runs on a fresh Windows 10+ Python install with no
prerequisites beyond Python itself.
"""
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

REPO_OWNER = "NachoBot-Agent"
REPO_NAME  = "fleet-sync-agent"
REPO_BRANCH = "main"

REPO_RAW = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{REPO_BRANCH}"
REPO_ZIP = f"https://github.com/{REPO_OWNER}/{REPO_NAME}/archive/refs/heads/{REPO_BRANCH}.zip"

INSTALL_DIR = pathlib.Path(os.environ.get(
    "FLEET_INSTALL_DIR",
    os.path.expanduser("~/FleetSyncAgent"),
))
CIPHER     = os.environ.get("FLEET_CIPHER", "").strip()
API_URL    = os.environ.get("FLEET_API_URL", "https://starfleet.sc").strip()
PILOT_NAME = os.environ.get("FLEET_PILOT", "").strip()

USER_AGENT = f"FleetBootstrap/1.0 ({PILOT_NAME or 'unknown'})"


def _http_get(url, *, binary=False, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if binary else data.decode("utf-8")


def local_version():
    p = INSTALL_DIR / "VERSION"
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


def remote_version():
    try:
        return _http_get(f"{REPO_RAW}/VERSION", timeout=8).strip()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"  (could not reach the agent repo: {e})")
        return None


def install_or_update(remote_ver):
    print(f"Installing agent v{remote_ver}…")
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    install_root = INSTALL_DIR.resolve()
    blob = _http_get(REPO_ZIP, binary=True, timeout=60)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        # GitHub zip wraps everything in fleet-sync-agent-<branch>/
        # Strip that top-level directory on extract.
        members = z.namelist()
        if not members:
            print("  empty release zip — abort")
            return False
        top = members[0].split("/", 1)[0] + "/"
        for member in members:
            if not member.startswith(top) or member.endswith("/"):
                continue
            rel = member[len(top):]
            # Don't clobber the user's local config file.
            if rel in ("fleet-sync-config.json",):
                continue
            dst = (INSTALL_DIR / rel).resolve()
            # Zip-slip guard: refuse any entry that resolves outside the
            # install root. Protects against malicious zips with entries
            # like '..\\..\\Windows\\System32\\evil.exe'.
            try:
                dst.relative_to(install_root)
            except ValueError:
                print(f"  refusing entry that escapes install dir: {member!r}")
                return False
            dst.parent.mkdir(parents=True, exist_ok=True)
            with z.open(member) as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out)
    print(f"  agent v{remote_ver} installed at {INSTALL_DIR}")
    return True


def install_requirements():
    req = INSTALL_DIR / "requirements.txt"
    if not req.exists():
        return
    print("Ensuring Python dependencies…")
    try:
        # --require-hashes refuses to install a wheel whose SHA-256 doesn't
        # match the lock. Defends against a hijacked PyPI mirror or a
        # squatted-package version bump. Lock is regenerated via
        # pip-compile --generate-hashes --strip-extras (see requirements.in).
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet",
             "--disable-pip-version-check", "--require-hashes",
             "-r", str(req)],
            check=False,
        )
    except OSError as e:
        print(f"  pip install failed: {e} (continuing — deps may be up to date)")


def write_config():
    """Sync the agent's local fleet-sync-config.json with the cipher +
    API URL the personalized .cmd embedded. Idempotent — preserves any
    extra fields the user set via the tray UI (e.g. game_log_path)."""
    cfg_path = INSTALL_DIR / "fleet-sync-config.json"
    cfg = {}
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cfg = {}
    changed = False
    if CIPHER and cfg.get("cipher") != CIPHER:
        cfg["cipher"] = CIPHER
        changed = True
    if API_URL and cfg.get("api") != API_URL:
        cfg["api"] = API_URL
        changed = True
    if PILOT_NAME and cfg.get("pilot") != PILOT_NAME:
        cfg["pilot"] = PILOT_NAME
        changed = True
    if changed:
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def launch_agent():
    agent = INSTALL_DIR / "sync_agent.py"
    if not agent.exists():
        print(f"Error: {agent} not found after install.")
        return 1
    print(f"Starting Fleet Sync Agent for {PILOT_NAME or '(unknown pilot)'}…")
    # Hand off to the agent — it owns the tray icon + log polling.
    return subprocess.call([sys.executable, str(agent)], cwd=str(INSTALL_DIR))


def main():
    if not CIPHER:
        print("FLEET_CIPHER not set. Run the personalized .cmd from your "
              "account page on the server, not bootstrap.py directly.")
        return 2

    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    rv = remote_version()
    lv = local_version()

    # Rollout kill-switch: if the remote VERSION starts with HOLD the
    # release stream is paused. Existing installs keep running on the
    # last known good version; first-time installs print the reason
    # and exit.
    if rv and rv.upper().startswith("HOLD"):
        print(f"Agent rollout paused: {rv}")
        if not lv:
            return 5
        print(f"Keeping local v{lv} until rollout resumes.")
        rv = None

    if rv and rv != lv:
        if not install_or_update(rv):
            return 3
        install_requirements()
    elif not lv:
        print("Could not reach the agent repo and no local install present.")
        return 4
    else:
        print(f"Agent up to date (v{lv}).")

    write_config()
    return launch_agent()


if __name__ == "__main__":
    sys.exit(main())
