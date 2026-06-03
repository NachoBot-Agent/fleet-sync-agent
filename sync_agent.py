"""Star Citizen captain sync agent.

Watches the Star Citizen Game.log, parses captain state (name, status,
location, ship, route, conditions) and POSTs the parsed payload to
starfleet.sc / api/report with the user's account cipher.

UI:
- System tray icon (pystray) showing connected / paused / error.
- Settings window (tkinter) for the server URL + account cipher + the
  Star Citizen LIVE folder path.

Windows-only. Game.log is read from
    C:\\Program Files\\Roberts Space Industries\\StarCitizen\\LIVE\\Game.log
by default; configurable via the settings window or the SC_LIVE_DIR env
var.

Runtime deps (pip install on the gaming PC):
    pip install pystray Pillow

stdlib only otherwise — urllib for HTTP, tkinter for the settings
window, threading for the log watcher.

Author: NachoBot Agent
"""
import json
import os
import platform
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── Configuration ────────────────────────────────────────────────────

if platform.system() == "Windows":
    DEFAULT_LIVE_DIR = Path(r"C:\Program Files\Roberts Space Industries\StarCitizen\LIVE")
else:
    # macOS / Linux — accepts a custom path via SC_LIVE_DIR for development
    DEFAULT_LIVE_DIR = Path(os.environ.get("SC_LIVE_DIR", str(Path.home() / "sc-livedir-stub")))

CONFIG_PATH = Path(__file__).with_name("fleet-sync-config.json")
DEFAULT_FLEET_API = "https://starfleet.sc"

POLL_INTERVAL_SEC = 2.0      # how often to re-read Game.log
HEARTBEAT_SEC = 45           # republish even without changes after this
USER_AGENT = "fleet-sync-agent/1.0"

# Game.log can grow to hundreds of MB over a long session. We only ever
# need recent context (last spawn, latest equipment, current location),
# so cap the read at the trailing 16 MB. _read_tail() drops the first
# (likely partial) line of the tail so we never half-parse a record.
GAME_LOG_TAIL_BYTES = 16 * 1024 * 1024


def _read_tail(path, max_bytes):
    """Return the trailing `max_bytes` of a text file as a decoded string.
    Drops the first line of the tail when truncation actually happened so
    the parser doesn't trip on a half-record."""
    size = path.stat().st_size
    if size <= max_bytes:
        return path.read_text(encoding="utf-8", errors="replace")
    with open(path, "rb") as f:
        f.seek(size - max_bytes)
        blob = f.read()
    text = blob.decode("utf-8", errors="replace")
    nl = text.find("\n")
    return text[nl + 1:] if nl != -1 else text

# Captain report field allowlist (matches the server's allowlist)
ALLOWED_REPORT_FIELDS = {"captain", "status", "location", "ship", "route",
                         "conditions", "reportedAt", "source"}
ALLOWED_PLACE_FIELDS = {"label", "code", "base", "planet", "system",
                        "confidence", "uuid", "coordinates"}
ALLOWED_SHIP_FIELDS = {"className", "label", "career", "role", "manufacturer",
                       "entityId"}
ALLOWED_ROUTE_FIELDS = {"code", "label"}
ALLOWED_CONDITION_FIELDS = {"monitored", "armistice", "jurisdiction"}

FRIENDLY_LOCATIONS = {
    "RR_ARC_L1": "ARC-L1 Wide Forest Station",
    "RR_ARC_L3": "ARC-L3 Modern Express Station",
    "Stanton3_Area18": "Area18, ArcCorp",
    "Nyx_Levski": "Levski, Nyx",
    "RR_JP_NyxPyro": "Pyro-Nyx Jump Point Station",
    "Stanton4_RayariHydro_Deltana": "Rayari Deltana Research Outpost",
}
FRIENDLY_TARGETS = {
    "OOC_Stanton": "Stanton",
    "OOC_Stanton_3_ArcCorp": "ArcCorp",
    "LOC_RR_S3_L1": "ARC-L1",
    "LOC_RR_S3_L3": "ARC-L3",
}


# ── Config persistence ──────────────────────────────────────────────

def load_config():
    defaults = {"api": DEFAULT_FLEET_API,
                "cipher": "",
                "live_dir": str(DEFAULT_LIVE_DIR),
                "enabled": True}
    if not CONFIG_PATH.exists():
        return defaults
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return defaults
    # Merge defaults under whatever was on disk so a partial config (e.g.
    # the one bootstrap writes — cipher + api + pilot only) still gets
    # `enabled: True` and a usable `live_dir`. Without this the agent
    # silently starts paused on every fresh install.
    return {**defaults, **loaded}


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


# ── Game.log parser ─────────────────────────────────────────────────

def _stamp(line):
    m = re.match(r"^<([^>]+)>", line)
    return m.group(1) if m else None


def _stamp_ms(line):
    try:
        from datetime import datetime
        return datetime.fromisoformat((_stamp(line) or "").replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return 0


def _learn_locations(game_log_path):
    """Walk the log + adjacent backups to learn location_id → readable code mappings."""
    learned = {}
    try:
        files = list(game_log_path.parent.glob("logbackups/*.log")) + [game_log_path]
    except OSError:
        files = [game_log_path]
    for file_path in files:
        if not file_path.exists():
            continue
        candidate, candidate_at = None, 0
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = re.search(r"<Update Inventory Location>.*Landing \[\d+\] -> \[(\d+)\]\. Location \[\d+\] -> \[(\d+)\]", line)
            if m:
                landing, location = m.groups()
                if landing != "0" and landing == location:
                    candidate, candidate_at = landing, _stamp_ms(line)
                continue
            m = re.search(r"<RequestLocationInventory>.*Location\[([^\]]+)\]", line)
            if m and candidate and _stamp_ms(line) - candidate_at <= 90000:
                code = m.group(1)
                learned[candidate] = {"id": candidate, "code": code,
                                      "label": FRIENDLY_LOCATIONS.get(code, code.replace("_", " ")),
                                      "confidence": "correlated"}
    return learned


def _make_event(time, event_type, title, detail="", tone="neutral"):
    return {"time": time, "type": event_type, "title": title, "detail": detail, "tone": tone}


def parse_game_log(text, game_log_path=None):
    """Walk the log forward and derive the captain's full state.

    Returns the rich payload the central console renders: captain
    (under session.player), status, location, ship, route, conditions,
    equipment, recent events, entitlements, learned location map,
    session metadata, plus log freshness. The server enriches further
    (place, services, equipment thumbnails, ship resolution).
    """
    learned = _learn_locations(game_log_path) if game_log_path else {}

    # Equipment tracking: Game.log emits AttachmentReceived events when
    # items are equipped, but no symmetric "AttachmentRemoved" event. The
    # original implementation accumulated forever, so anything the player
    # ever wore stuck around. New strategy — attachments accumulate into
    # `pending`; on each OnClientSpawned event we commit pending as the
    # current loadout. After the loop ends, any post-final-spawn picks
    # (mid-gameplay swaps) merge into the committed loadout.
    equipment = {}
    pending_equipment = {}
    recent = []
    decoded_seen = set()
    player = environment = session_started = None
    location_id = landing_id = readable_location = route = ship = None
    entitlements = refinery_notice = None
    asop_updated_at = None
    spawned = asop_warning = False
    monitored = armistice = jurisdiction = None
    vehicle_names = {}
    stowed_vehicles = []

    for line in text.splitlines():
        if not line:
            continue
        time = _stamp(line)

        m = re.search(r"<Init> Process sc-client started.*Env:\s*([A-Za-z][\w-]*)", line)
        if m:
            environment, session_started = m.group(1), time
            recent.append(_make_event(time, "SESSION", "Client started", environment, "cyan"))
            continue

        m = re.search(r"<AttachmentReceived> Player\[([^\]]+)\] Attachment\[[^,]+,\s*([^,]+),\s*\d+\].* Port\[([^\]]+)\]", line)
        if m:
            player, item, port = m.groups()
            if port == "Body_ItemPort":
                pending_equipment.clear()
                equipment.clear()
            if re.match(r"^(Armor_|wep_|backpack|medPen|oxyPen|magazine)", port):
                pending_equipment[port] = item
            continue

        # <StoreItem> is the unequip signal. Format:
        #   <StoreItem> Request[N] store '<class>_<entity_id>' [<entity_id>] by ...
        # The single-quoted name is class+entity glued together; the trailing
        # _<digits> is the entity instance. Strip it to get the class name and
        # remove whichever port (in either dict) currently holds that class.
        m = re.search(r"<StoreItem> Request\[\d+\] store '([^']+)'", line)
        if m:
            stored = m.group(1)
            class_name = re.sub(r"_\d+$", "", stored)
            for d in (equipment, pending_equipment):
                for port, item_class in list(d.items()):
                    if item_class == class_name:
                        del d[port]
            continue

        if "[CSessionManager::OnClientSpawned] Spawned!" in line:
            spawned = True
            # Commit this spawn cycle's pending attachments as the current
            # loadout, then reset pending so the next cycle starts clean.
            # Any stale items from a previous spawn that weren't re-emitted
            # by the game are correctly dropped here.
            equipment = pending_equipment
            pending_equipment = {}
            recent.append(_make_event(time, "SESSION", "Player spawned", "", "green"))
            continue

        m = re.search(r"<Update Inventory Location> Player \[([^\]]+)\].*Landing \[(\d+)\] -> \[(\d+)\]\. Location \[(\d+)\] -> \[(\d+)\]", line)
        if m:
            player, _, landing_id, _, location_id = m.groups()
            decoded = learned.get(location_id)
            title = decoded["label"] if decoded else "Location transition detected"
            detail = "Decoded from local log history." if decoded else "Awaiting a readable local context."
            recent.append(_make_event(time, "LOCATION", title, detail, "magenta" if decoded else "neutral"))
            continue

        m = re.search(r"<RequestLocationInventory> Player\[([^\]]+)\] requested inventory for Location\[([^\]]+)\]", line)
        if m:
            player, code = m.groups()
            label = FRIENDLY_LOCATIONS.get(code, code.replace("_", " "))
            readable_location = {"code": code, "label": label, "confidence": "direct"}
            if code not in decoded_seen:
                decoded_seen.add(code)
                recent.append(_make_event(time, "LOCATION", label, "Direct readable local context.", "magenta"))
                recent.append(_make_event(time, "ARRIVAL", f"Arrived: {label}", "Readable local inventory context established.", "cyan"))
            continue

        m = re.search(r"Player has (?:requested fuel calculation to destination|selected point) (\S+)", line)
        if m:
            code = m.group(1)
            label = FRIENDLY_TARGETS.get(code, code.replace("_", " "))
            next_route = {"code": code, "label": label}
            if route != next_route:
                route = next_route
                recent.append(_make_event(time, "ROUTE", f"Quantum target: {label}", "Navigation target selected.", "cyan"))
            continue

        if "<Local Route Guard - Server Rerouted>" in line:
            continue

        m = re.search(r"\|\s*([A-Za-z0-9_]+_\d+)\[\d+\]\|CSCItemNavigation", line)
        if m:
            runtime = m.group(1)
            entity_match = re.search(r"_(\d+)$", runtime)
            entity_id = entity_match.group(1) if entity_match else None
            class_name = re.sub(r"_\d+$", "", runtime)
            next_ship = {"className": class_name, "label": class_name.replace("_", " "), "entityId": entity_id}
            vehicle_names[entity_id] = next_ship
            if not ship or next_ship["className"] != (ship or {}).get("className"):
                ship = next_ship
                recent.append(_make_event(time, "SHIP", next_ship["label"], "Active navigation context.", "green"))
            continue

        m = re.search(r"\[STOWING ON UNREGISTER\].*Attempting to stow current vehicle \[(\d+)\]", line)
        if m:
            entity_id = m.group(1)
            stowed_vehicles.append({"time": time, "entityId": entity_id})
            if ship and ship.get("entityId") == entity_id:
                recent.append(_make_event(time, "SHIP", "Navigation ship stowed", ship["label"], "neutral"))
                ship = None
            continue

        m = re.search(r"<VehicleListQuery>.*Retrieved (\d+) entitlements out of (\d+)", line)
        if m:
            retrieved, total = map(int, m.groups())
            entitlements = {"retrieved": retrieved, "total": total}
            asop_updated_at = time
            recent.append(_make_event(time, "ASOP", f"{retrieved} vehicle entitlements loaded", f"{retrieved}/{total}", "green"))
            continue

        if "Ship Locations Query results don't match" in line:
            asop_warning = True
            recent.append(_make_event(time, "ASOP", "Partial ship-location response",
                                      "The game service did not return every requested ship location.", "amber"))
            continue

        m = re.search(r'<SHUDEvent_OnNotification> Added notification "([^"]+)"', line)
        if m:
            notice = m.group(1).strip()
            if not notice:
                continue
            if notice.startswith("Entered Monitored Space"): monitored = True
            if notice.startswith("Exited Monitored Space"): monitored = False
            if notice.startswith("Entering Armistice"): armistice = True
            if notice.startswith("Leaving Armistice"): armistice = False
            if notice.startswith("Entered UEE Jurisdiction"): jurisdiction = "UEE"
            if notice.startswith("Exited UEE Jurisdiction"): jurisdiction = None
            if "Refinery Work Order" in notice:
                refinery_notice = notice.rstrip(":")
            recent.append(_make_event(time, "NOTICE", notice.rstrip(":"), "",
                                       "green" if "Completed" in notice else "neutral"))
            continue

        m = re.search(r"<SystemQuit>.*reason=([^,]+)", line)
        if m:
            spawned = False
            ship = None
            recent.append(_make_event(time, "SESSION", "Client quit", m.group(1), "amber"))

    if not player:
        return None

    # Build the rich payload — same shape as the legacy build_state() so the
    # central console can render every panel the old local dashboard did.
    # Server-side enrichment (place, services, equipment thumbnails, ship
    # resolution) happens after upload via sc_data.py.
    learned_list = sorted(learned.values(), key=lambda x: x["label"])
    location = readable_location or learned.get(location_id) or (
        {"id": location_id, "label": f"Unresolved location {location_id}",
         "code": None, "confidence": "opaque"} if location_id else {})
    location.update({"id": location_id, "landingId": landing_id,
                     "decodedCount": len(learned_list), "learned": learned_list})

    # Log freshness — read mtime if we have the path
    log_info = {"exists": False, "bytes": 0, "modifiedAt": None, "backupCount": 0}
    if game_log_path and game_log_path.exists():
        try:
            stat = game_log_path.stat()
            log_info = {
                "exists": True,
                "bytes": stat.st_size,
                "modifiedAt": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                "backupCount": max(len(list(game_log_path.parent.glob("logbackups/*.log"))), 0),
            }
        except OSError:
            pass

    return {
        # Lean fields the central console roster uses
        "captain": player,
        "status": "active" if spawned else "offline",
        "location": location if location.get("code") else {"label": location.get("label")},
        "ship": ship,
        "route": route,
        "conditions": {"monitored": monitored, "armistice": armistice, "jurisdiction": jurisdiction},
        "reportedAt": datetime.now(timezone.utc).isoformat(),
        "source": "sync-agent",

        # Rich fields the captain dashboard renders
        "session": {"player": player, "environment": environment,
                    "startedAt": session_started, "spawned": spawned},
        # Merge any attachments emitted AFTER the last spawn marker
        # (mid-game pickups) on top of the committed loadout — these are
        # genuine changes, not stale carryover from a prior spawn.
        "equipment": {**equipment, **pending_equipment},
        "equipmentNote": "Game.log exposes equipped attachment points, including stocked backpack attachments. It requests backpack inventory data but does not print the returned storage contents.",
        "recent": list(reversed(recent[-80:])),
        "entitlements": entitlements,
        "refineryNotice": refinery_notice,
        "asopWarning": asop_warning,
        "asopUpdatedAt": asop_updated_at,
        "stowedVehicles": stowed_vehicles[-8:],
        "log": log_info,
    }


# ── HTTP publish ────────────────────────────────────────────────────

def publish_report(api, cipher, report):
    """POST a report. Returns (status_code, response_text). Raises on
    network errors so callers can surface them in the tray icon."""
    body = json.dumps(report).encode()
    req = urllib.request.Request(
        f"{api.rstrip('/')}/api/report",
        data=body, method="POST",
        headers={
            "Authorization": f"Bearer {cipher}",
            "Content-Type": "application/json",
            # Required to bypass Cloudflare Browser Integrity Check;
            # default Python-urllib UA is blocked with HTTP 1010/403.
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# ── Agent runtime ───────────────────────────────────────────────────

class Agent:
    def __init__(self):
        self.cfg = load_config()
        self.state = "starting"  # starting | connected | paused | error | no-log
        self.last_publish_at = None
        self.last_error = None
        self.last_report_fingerprint = None
        self.last_captain = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()

    def _set_state(self, new_state):
        """Update self.state and emit a one-line stdout message on transitions
        (state change OR captain change). Keeps the terminal narrating
        without spamming during steady-state polls."""
        key = (new_state, self.last_captain, self.last_error if new_state == "error" else None)
        if getattr(self, "_last_state_key", None) == key:
            self.state = new_state
            return
        self._last_state_key = key
        self.state = new_state
        ts = datetime.now().strftime("%H:%M:%S")
        api = self.cfg.get("api", "?")
        if new_state == "connected":
            if self.last_captain:
                print(f"[{ts}] running · publishing {self.last_captain} -> {api}", flush=True)
            else:
                print(f"[{ts}] running · Game.log readable, waiting for captain to spawn in-game", flush=True)
        elif new_state == "paused":
            print(f"[{ts}] paused (right-click the tray icon -> Resume sync)", flush=True)
        elif new_state == "no-log":
            print(f"[{ts}] Game.log not found at {self.cfg.get('live_dir')!s} -- open Settings", flush=True)
        elif new_state == "error":
            print(f"[{ts}] error: {self.last_error}", flush=True)
        elif new_state == "starting":
            print(f"[{ts}] starting...", flush=True)

    def status_text(self):
        if self.state == "connected":
            who = self.last_captain or "(captain not yet seen)"
            when = self.last_publish_at.strftime("%H:%M:%S") if self.last_publish_at else "—"
            return f"Connected · {who} · last post {when}"
        if self.state == "paused":
            return "Paused (toggle from menu to resume)"
        if self.state == "no-log":
            return f"Game.log not found at {self.cfg['live_dir']}"
        if self.state == "error":
            return f"Error: {self.last_error or 'see console'}"
        return "Starting…"

    def pause(self):
        with self.lock:
            self.cfg["enabled"] = False
            save_config(self.cfg)
            self._set_state("paused")

    def resume(self):
        with self.lock:
            self.cfg["enabled"] = True
            save_config(self.cfg)
            self._set_state("starting")

    def reload_config(self):
        with self.lock:
            self.cfg = load_config()
            self._set_state("starting")
            self.last_report_fingerprint = None

    def loop(self):
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                # str(e) on its own ('unhashable type: dict') is useless
                # for diagnosis — print the full traceback so the user can
                # paste it in support of a bug report. The state machine
                # still reflects the short message on the tray icon.
                self.last_error = f"{type(e).__name__}: {e}"
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] tick raised {type(e).__name__}: {e}", flush=True)
                traceback.print_exc(file=sys.stdout)
                sys.stdout.flush()
                self._set_state("error")
            self.stop_event.wait(POLL_INTERVAL_SEC)

    def _tick(self):
        with self.lock:
            cfg = dict(self.cfg)
        if not cfg.get("enabled"):
            self._set_state("paused")
            return
        if not cfg.get("cipher"):
            self.last_error = "No account cipher configured. Open Settings."
            self._set_state("error")
            return
        live_dir = Path(cfg["live_dir"])
        game_log = live_dir / "Game.log"
        if not game_log.exists():
            self._set_state("no-log")
            return
        try:
            text = _read_tail(game_log, GAME_LOG_TAIL_BYTES)
        except OSError as e:
            self.last_error = f"Cannot read Game.log: {e}"
            self._set_state("error")
            return
        report = parse_game_log(text, game_log)
        if not report or not report.get("captain"):
            self._set_state("connected")  # connected but no signal yet
            return

        # Skip if unchanged AND inside heartbeat window — fingerprint excludes
        # mtime/timestamp fields so we don't republish every poll
        fingerprint = json.dumps(
            {k: v for k, v in report.items() if k not in ("reportedAt", "log", "recent")},
            sort_keys=True, default=str)
        now = time.monotonic()
        if (fingerprint == self.last_report_fingerprint and self.last_publish_at and
                (now - getattr(self, "_last_publish_mono", 0)) < HEARTBEAT_SEC):
            self._set_state("connected")
            return

        status, body = publish_report(cfg["api"], cfg["cipher"], report)
        if status in (200, 202):
            self.last_publish_at = datetime.now()
            self.last_captain = report["captain"]
            self.last_report_fingerprint = fingerprint
            self._last_publish_mono = now
            self.last_error = None
            self._set_state("connected")
        elif status == 401:
            self.last_error = "Account cipher rejected (401). Open Settings and paste the cipher from your account page."
            self._set_state("error")
        else:
            self.last_error = f"Server returned HTTP {status}: {body[:200]}"
            self._set_state("error")


# ── Settings window (tkinter) ──────────────────────────────────────

def open_settings(agent):
    import tkinter as tk
    from tkinter import filedialog, messagebox

    win = tk.Tk()
    win.title("Fleet Sync Agent — Settings")
    win.geometry("520x320")
    win.configure(bg="#06151b")

    style = {"bg": "#06151b", "fg": "#e7f5f7"}
    label_kw = {"bg": "#06151b", "fg": "#78939a", "anchor": "w"}
    entry_kw = {"bg": "#031015", "fg": "#e7f5f7", "insertbackground": "#e7f5f7",
                "relief": "flat", "highlightthickness": 1, "highlightbackground": "#54b8cb",
                "highlightcolor": "#8fe6ef"}

    tk.Label(win, text="Server URL URL", **label_kw).pack(fill="x", padx=20, pady=(20, 4))
    api_var = tk.StringVar(value=agent.cfg.get("api", DEFAULT_FLEET_API))
    tk.Entry(win, textvariable=api_var, **entry_kw).pack(fill="x", padx=20)

    tk.Label(win, text="Account cipher (from your account page on the server)", **label_kw).pack(fill="x", padx=20, pady=(14, 4))
    cipher_var = tk.StringVar(value=agent.cfg.get("cipher", ""))
    tk.Entry(win, textvariable=cipher_var, show="*", **entry_kw).pack(fill="x", padx=20)

    tk.Label(win, text="Star Citizen LIVE folder", **label_kw).pack(fill="x", padx=20, pady=(14, 4))
    livedir_var = tk.StringVar(value=agent.cfg.get("live_dir", str(DEFAULT_LIVE_DIR)))
    row = tk.Frame(win, bg="#06151b")
    row.pack(fill="x", padx=20)
    tk.Entry(row, textvariable=livedir_var, **entry_kw).pack(side="left", fill="x", expand=True)

    def browse():
        chosen = filedialog.askdirectory(initialdir=livedir_var.get() or str(Path.home()))
        if chosen:
            livedir_var.set(chosen)
    tk.Button(row, text="Browse", command=browse,
              bg="#06151b", fg="#8fe6ef", activebackground="#163844",
              relief="flat", padx=10).pack(side="left", padx=(8, 0))

    msg_var = tk.StringVar(value="")
    tk.Label(win, textvariable=msg_var, **label_kw).pack(fill="x", padx=20, pady=(10, 0))

    def save():
        api = api_var.get().strip()
        cipher = cipher_var.get().strip()
        live_dir = livedir_var.get().strip()
        if not re.match(r"^https?://", api):
            msg_var.set("Server URL must start with http:// or https://")
            return
        if not re.match(r"^[a-f0-9]{64}$", cipher):
            msg_var.set("Cipher must be 64 hexadecimal characters")
            return
        agent.cfg.update({"api": api, "cipher": cipher, "live_dir": live_dir, "enabled": True})
        save_config(agent.cfg)
        agent.reload_config()
        msg_var.set("Saved. Agent reloaded.")

    def test():
        api = api_var.get().strip().rstrip("/")
        cipher = cipher_var.get().strip()
        if not (api and cipher):
            msg_var.set("Set Server URL + cipher first")
            return
        try:
            req = urllib.request.Request(f"{api}/api/me",
                                         headers={"Authorization": f"Bearer {cipher}",
                                                  "User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read())
            user = data.get("user", {})
            msg_var.set(f"OK — signed in as {user.get('email','?')}")
        except urllib.error.HTTPError as e:
            msg_var.set(f"Test failed: HTTP {e.code} {e.reason}")
        except Exception as e:
            msg_var.set(f"Test failed: {type(e).__name__}: {e}")

    btns = tk.Frame(win, bg="#06151b")
    btns.pack(fill="x", padx=20, pady=(20, 16))
    tk.Button(btns, text="Test connection", command=test,
              bg="#06151b", fg="#78939a", activebackground="#163844",
              relief="flat", padx=14, pady=8, borderwidth=1).pack(side="left")
    tk.Button(btns, text="Save", command=save,
              bg="#06151b", fg="#8fe6ef", activebackground="#163844",
              relief="flat", padx=14, pady=8, borderwidth=1).pack(side="right")

    win.mainloop()


# ── Tray icon ───────────────────────────────────────────────────────

def make_icon_image(state):
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color_map = {
        "connected": (143, 227, 187, 255),  # green
        "starting":  (143, 230, 239, 255),  # cyan
        "paused":    (120, 147, 154, 255),  # muted grey
        "no-log":    (224, 180, 119, 255),  # amber
        "error":     (224, 128, 119, 255),  # red
    }
    c = color_map.get(state, (120, 147, 154, 255))
    draw.ellipse((6, 6, size - 6, size - 6), outline=c, width=4)
    inner = 18
    draw.ellipse((inner, inner, size - inner, size - inner), fill=c)
    return img


def run_tray(agent):
    import pystray
    from pystray import MenuItem as Item, Menu

    icon = pystray.Icon("fleet-sync-agent", make_icon_image(agent.state),
                        "Fleet Sync Agent")

    def refresh(_=None):
        icon.icon = make_icon_image(agent.state)
        icon.title = "Fleet Sync Agent — " + agent.status_text()
        icon.menu = build_menu()

    def open_settings_thread(_=None):
        threading.Thread(target=open_settings, args=(agent,), daemon=True).start()

    def toggle_pause(_=None):
        if agent.state == "paused":
            agent.resume()
        else:
            agent.pause()
        refresh()

    def quit_app(_=None):
        agent.stop_event.set()
        icon.stop()

    def build_menu():
        pause_label = "Resume sync" if agent.state == "paused" else "Pause sync"
        return Menu(
            Item(lambda _: agent.status_text(), None, enabled=False),
            Menu.SEPARATOR,
            Item("Settings…", open_settings_thread),
            Item(pause_label, toggle_pause),
            Menu.SEPARATOR,
            Item("Quit", quit_app),
        )

    icon.menu = build_menu()

    def updater():
        last_state = None
        while not agent.stop_event.is_set():
            if agent.state != last_state:
                last_state = agent.state
                refresh()
            time.sleep(2)
    threading.Thread(target=updater, daemon=True).start()

    icon.run()


# ── Headless mode (no tray, just the watcher loop) ──────────────────

def run_headless(agent):
    print(f"Fleet Sync Agent — headless mode")
    print(f"  API:      {agent.cfg.get('api')}")
    print(f"  LIVE dir: {agent.cfg.get('live_dir')}")
    print(f"  Cipher:   {'set' if agent.cfg.get('cipher') else 'NOT SET — edit fleet-sync-config.json'}")
    last_state = None
    try:
        while not agent.stop_event.is_set():
            if agent.state != last_state:
                last_state = agent.state
                print(f"[{datetime.now().strftime('%H:%M:%S')}] {agent.state}: {agent.status_text()}")
            time.sleep(2)
    except KeyboardInterrupt:
        agent.stop_event.set()


# ── Entrypoint ──────────────────────────────────────────────────────

def main():
    agent = Agent()
    ts = datetime.now().strftime("%H:%M:%S")
    api = agent.cfg.get("api", "?")
    live_dir = agent.cfg.get("live_dir", "?")
    print(f"[{ts}] verifying configuration...", flush=True)
    print(f"          server   : {api}", flush=True)
    print(f"          Game.log : {live_dir}", flush=True)
    print(f"          cipher   : {'set' if agent.cfg.get('cipher') else 'MISSING'}", flush=True)
    print(f"          enabled  : {agent.cfg.get('enabled', False)}", flush=True)
    print(f"[{ts}] tray icon up · the agent is running.", flush=True)
    print(f"          Right-click the tray icon (system tray, bottom-right) for Settings, Pause, Quit.", flush=True)
    print(f"          State updates will appear below as they change.", flush=True)
    print(f"          Leave this window open; closing it stops the agent.", flush=True)
    watcher = threading.Thread(target=agent.loop, daemon=True)
    watcher.start()

    headless = "--headless" in sys.argv or os.environ.get("FLEET_AGENT_HEADLESS") == "1"
    if headless:
        run_headless(agent)
    else:
        try:
            run_tray(agent)
        except ImportError as e:
            print(f"[agent] pystray/Pillow not installed ({e}); falling back to headless.")
            print(f"[agent] Install with: pip install pystray Pillow")
            run_headless(agent)


if __name__ == "__main__":
    main()
