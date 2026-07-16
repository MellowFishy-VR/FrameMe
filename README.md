# FrameMe

> Background tray app that watches Steam Frame stock every minute and blasts a looping alert when you can reserve one.

Cross-platform **multi-source** desktop monitor for Steam Frame availability. It runs in the system tray, polls several independent sources on staggered schedules, and when a confirmed reservation signal appears it **loops an alert sound** until you click the notification to open the store page.

Inspired by [SteamFrameTracker](https://github.com/tomgamer22/SteamFrameTracker).

## Features

- Background monitoring from the system tray (Windows, Linux)
- Config-driven watchers in [`config.yaml`](config.yaml) (toggle without code changes)
- Tiered alerts (critical / quieter / digest)
- Transition-only alerts (persisted state under `~/.config/frameme/state/`)
- Append-only **change log** with unified diffs (`~/.config/frameme/changes.log`)
- Custom alert sound (WAV, MP3, OGG, FLAC)
- **Looping Tier 1 alert** until notification click
- Heartbeat: Tier 1 silent failure → `MONITORING BROKEN` alert
- `--test` dry-run for parser verification
- Steam Machine sanity check button (app `4165910`)

## Watchers

| ID | Tier | Interval | What |
|----|------|----------|------|
| `steam_frame_appdetails` | 1 | 60s | Steam `appdetails` API (app `4165890`) |
| `steam_frame_sale` | 1 | 60s | Hardware page via **Playwright** (scroll-load `sale-display`, same method as steamframe-check) |
| `komodo` | 1 | 5m | JP distributor stock/price/cart |
| `steamworks` | 1 | 2m | Steamworks announcements (keyword match) |
| `steam_pics` | 2 | 60s | Anonymous Steam PICS for `4165890` + `3990420` (persistent session) |
| `importgenius` | 3 | 6h | Shipment pages (best-effort; auto-disables if paywalled) |
| `fcc_valve` | 3 | 12h | FCC filings for grantee `2AES4` |
| `valve_compliance` | 3 | 6h | Valve hardware compliance FAQ via **Playwright** |

**PICS vs SteamDB:** we do **not** scrape steamdb.info (ToS / blocking). Alerts include SteamDB URLs as human “view details” links only. Data comes from Steam PICS via `steam[client]`. A persistent anonymous session avoids `TryAnotherCM` login thrash. Global Steam `change_number` ticks are tracked but **not** written to the change log unless tracked app/package payload actually changes.

**Sale page:** `steam_frame_sale` uses headless Chromium (Playwright) against `hardware/steamframe`, waits for `[data-featuretarget="sale-display"]`, runs two scroll passes so lazy sections mount, then hashes a stabilized snapshot of that region — same approach as `steamframe-check/steamframe_monitor.py`. Plain HTTP alone only sees an empty JS shell.

Hardening against false alerts from incomplete lazy-load:
- Strip volatile tracking params (`snr`, `curator_clanid`) and sort action links before hashing
- Ignore slightly shorter captures that have **no** substantive line-level diff (incomplete scroll); real edits that shrink the page still alert
- Trivial/noise fingerprint flips update state but do not alert or log

## Alert tiers

| Tier | Behavior |
|------|----------|
| **1** | Looping sound + critical notification |
| **2** | Notification only (no sound loop) |
| **3** | Batched digest (every 30 min or ≥3 items) |

**Auto-stop monitoring** only on confirmed Frame reserve/purchase signals:
- `steam_frame_appdetails` becomes purchasable
- Sale page CTA or price appears

Steamworks keyword hits, Komodo leads, Tier 2/3 do **not** stop the monitor.

## Requirements

- Python 3.10+
- System tray desktop environment
- Linux: `libnotify` / notification daemon

```bash
cd frameme
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium   # required for steam_frame_sale scroll-capture
```

## Run

```bash
python main.py
```

Close the window to keep monitoring in the tray. Quit from the tray menu.

### Dry-run (verify parsers)

```bash
python main.py --test
```

Runs every enabled watcher once, prints extracted fields + stored state, sends **no** alerts.

## Config

Edit [`config.yaml`](config.yaml) to enable/disable sources, change intervals, or tweak heartbeat / digest settings.

State files: `~/.config/frameme/state/<watcher_id>.json`

### Change log

Path: `~/.config/frameme/changes.log`

Append-only unified diffs for **substantive** watcher transitions (and fired alerts). It does **not** log every poll:

- Unchanged fingerprints → silent
- Suppressed noise / normalize-only rewrites → silent
- PICS global `change_number`-only bumps → silent
- Real content diffs (sale page text, appdetails, tracked PICS payload, etc.) → logged

## Alert behavior (Tier 1)

1. Sound **loops** until dismissed  
2. Desktop notification appears  
3. **Click notification** → stop sound + open URL  
4. Or use **Stop alert** in the UI  
5. On confirmed purchase/reserve signal → monitoring stops automatically  

## Autostart (optional)

**Linux** — `~/.config/systemd/user/frameme.service`:

```ini
[Unit]
Description=FrameMe Steam Frame tracker
After=graphical-session.target

[Service]
ExecStart=%h/Development/frameme/.venv/bin/python %h/Development/frameme/main.py
Restart=on-failure

[Install]
WantedBy=graphical-session.target
```

```bash
systemctl --user enable --now frameme.service
```

**Windows** — shortcut to `pythonw.exe main.py` in Startup.

## License

Personal use.
