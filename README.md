# FrameMe

> Background tray app that watches Steam Frame stock every minute and blasts a looping alert when you can reserve one.

Cross-platform desktop app that monitors **Steam Frame** availability on the Steam store. It runs in the system tray, checks every minute, and when the Frame becomes available it **loops an alert sound** until you click the notification to stop it and open the reservation page.

Inspired by [SteamFrameTracker](https://github.com/tomgamer22/SteamFrameTracker).

## Features

- Background monitoring from the system tray (Windows, Linux)
- Checks every 60 seconds via the public Steam `appdetails` API
- Custom alert sound (WAV, MP3, OGG, FLAC)
- **Looping alert** — sound repeats until you dismiss it
- **Clickable notification** — stops the loop and opens the [Steam Frame store page](https://store.steampowered.com/hardware/steamframe) so you can reserve
- **Stop alert** button — silence the loop without opening the browser
- Live log of all checks
- **Test alert** — triggers the full looping alert + notification (no need to wait for stock)
- **Test check (Steam Machine)** — validates the API against app `4165910` (same hardware store flow as Steam Frame)

## Requirements

- Python 3.10+
- A desktop environment with a system tray (KDE, GNOME with tray extension, XFCE, Windows, etc.)
- Linux: `libnotify` / notification daemon (usually preinstalled)

### Linux packages (if notifications or tray fail)

**Arch / CachyOS:**

```bash
sudo pacman -S python python-pyside6 libnotify
```

**Ubuntu / Debian:**

```bash
sudo apt install python3 python3-pip python3-pyside6.qtcore python3-pyside6.qtgui \
  python3-pyside6.qtwidgets python3-pyside6.qtmultimedia libnotify-bin
```

## Install

```bash
cd frameme
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python main.py
```

Close the window to keep monitoring in the tray. Use the tray icon menu to show the window or quit.

## Alert behavior

When Steam Frame becomes available (or opens for pre-order):

1. The alert sound **loops continuously** until dismissed
2. A desktop notification appears
3. **Click the notification** → stops the sound and opens the Steam Frame reservation page
4. Or use **Stop alert** in the app window to silence the sound without opening the browser
5. **Monitoring stops automatically** — no more API checks after the alert fires

Use **Test alert** to preview the full flow anytime (monitoring is not stopped by test alerts).

## How availability is detected

Uses `https://store.steampowered.com/api/appdetails?appids=4165890` (same logic as SteamFrameTracker):

| Status | Condition |
|--------|-----------|
| **Available** | Not coming soon + price + purchase packages |
| **Pre-order** | Coming soon + purchase packages |
| **Not available** | Otherwise |

An alert fires only when status changes **into** available or pre-order (not on every check). Monitoring then stops automatically so you can focus on reserving.

## Autostart (optional)

**Linux (systemd user service)** — save as `~/.config/systemd/user/frameme.service`:

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

**Windows** — create a shortcut to `pythonw.exe main.py` in `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`.

## License

Personal use.
