# Star Citizen captain sync agent

A lightweight Windows agent that parses Star Citizen's `Game.log` and
publishes the live captain state to [starfleet.sc](https://starfleet.sc).

## Install

1. Sign in to [starfleet.sc](https://starfleet.sc) → Account.
2. Click **Download agent**. You get a file named like
   `<your-pilot-name> Agent.cmd` with your account cipher embedded.
3. Save it anywhere (Desktop is fine) and double-click to run.

The `.cmd` file is the only thing on disk that never changes. Every
time you run it, it fetches the latest `bootstrap.py` from this repo,
which in turn pulls the latest agent if it's behind, installs any
Python dependencies, writes your cipher into the local config, and
launches the tray agent.

Requirements on the gaming PC:
- **Windows 10 or later** (uses `curl`, bundled with Windows 10+).
- **Python 3.10 or later** on `PATH`. Install from
  [python.org](https://www.python.org/downloads/) if not present — the
  `.cmd` checks for it and gives you the link.

## How it works

```
<Pilot> Agent.cmd            ← downloaded once, never changes
        │
        ├─ curl  bootstrap.py from this repo (always fresh)
        │
        └─ python bootstrap.py
                  │
                  ├─ compare local VERSION vs this repo's VERSION
                  ├─ if behind, download repo ZIP, extract to %USERPROFILE%\FleetSyncAgent
                  ├─ pip install -r requirements.txt
                  ├─ write fleet-sync-config.json (cipher + server URL)
                  └─ python sync_agent.py
```

The agent itself reads `fleet-sync-config.json` for its server endpoint
and cipher, parses `Game.log` periodically, and POSTs the captured
state to `/api/report` on the server.

## Repo layout

- `bootstrap.py` — fetched fresh on every launch by the user's `.cmd`.
  Owns version comparison, install, dependency check, agent launch.
- `sync_agent.py` — the agent itself. Tray icon + log poller + POST
  client.
- `VERSION` — single line, the canonical release version. Bump when
  shipping a new agent.
- `requirements.txt` — pip deps.
- `tests/test_agent.py` — unit tests for the parser.

## Releasing

1. PR your change to `main`.
2. Bump `VERSION` in the same PR.
3. Merge. Next time any user launches their `.cmd`, bootstrap sees the
   new version and pulls it.

There's no separate release artifact — `main` IS the release stream.

## Privacy / what gets sent

The agent sends a captain state snapshot derived from `Game.log`:
- Current location (decoded from log)
- Active ship + route
- Equipped attachment ports (helmet, undersuit, magazines, medpens…)
- Recent log events (jurisdiction transitions, hangar requests, etc)
- Session state (spawned in world, environment tag, log freshness)

It does NOT send the raw `Game.log`, character creator data, friends
list, or anything from your RSI account.

## Roll-back / kill switch

If a bad release lands, edit `VERSION` to start with the literal string
`HOLD` (e.g. `HOLD-2026-06-02-revert`) and merge to `main`. The
bootstrap detects that prefix and exits cleanly with a "rollout
paused" message; existing installs keep running on the last known
good version.

## License

Public source for transparency. Use at your own risk.
