#!/usr/bin/env python3
"""FrameMe — Steam Frame availability monitor for Windows and Linux."""

from __future__ import annotations

import os
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QIcon
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from steam_checker import (
    STEAM_FRAME_APP_ID,
    STEAM_FRAME_URL,
    STEAM_MACHINE_APP_ID,
    CheckResult,
    ProductStatus,
    check_availability,
)

CHECK_INTERVAL_MS = 60_000
APP_NAME = "FrameMe"
ORG_NAME = "FrameMe"
SETTINGS_SOUND = "sound_path"
SETTINGS_MONITORING = "monitoring_enabled"
SETTINGS_LAST_STATUS = "last_frame_status"


def config_dir() -> Path:
    base = Path.home() / ".config" / "frameme"
    base.mkdir(parents=True, exist_ok=True)
    return base


def default_sound_path() -> Path | None:
    candidates = [
        Path(__file__).resolve().parent / "assets" / "alert.wav",
        Path("/usr/share/sounds/freedesktop/stereo/complete.oga"),
        Path("/usr/share/sounds/freedesktop/stereo/message.oga"),
        Path("/usr/share/sounds/freedesktop/stereo/bell.oga"),
    ]
    if sys.platform == "win32":
        windir = Path(os.environ.get("WINDIR", "C:\\Windows"))
        candidates.extend(
            [
                windir / "Media" / "Windows Notify System Generic.wav",
                windir / "Media" / "Windows Notify Calendar.wav",
            ]
        )
    for path in candidates:
        if path.is_file():
            return path
    return None


class NotificationService:
    """Desktop notifications with click-to-open support."""

    def __init__(self) -> None:
        self._loop = None
        self._notifier = None
        self._worker_ready = threading.Event()
        self._tray_click_handler = None
        self._use_desktop_notifier = False

        try:
            from desktop_notifier import DesktopNotifier, Urgency

            self._urgency = Urgency.Critical
            self._start_worker(DesktopNotifier)
            self._use_desktop_notifier = True
        except ImportError:
            self._urgency = None

    def _start_worker(self, notifier_cls) -> None:
        def worker() -> None:
            import asyncio

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._notifier = notifier_cls(app_name=APP_NAME)
            self._worker_ready.set()
            loop.run_forever()

        thread = threading.Thread(target=worker, daemon=True, name="frameme-notify")
        thread.start()
        if not self._worker_ready.wait(timeout=10):
            self._use_desktop_notifier = False

    async def _send_async(self, title: str, message: str, on_clicked) -> None:
        assert self._notifier is not None

        await self._notifier.send(
            title=title,
            message=message,
            urgency=self._urgency,
            on_clicked=on_clicked,
        )

    def send(
        self,
        title: str,
        message: str,
        tray: QSystemTrayIcon | None,
        on_clicked,
    ) -> None:
        if self._use_desktop_notifier and self._loop is not None:
            import asyncio

            asyncio.run_coroutine_threadsafe(
                self._send_async(title, message, on_clicked),
                self._loop,
            )
            return

        if tray is not None and tray.isSystemTrayAvailable():
            if self._tray_click_handler is not None:
                try:
                    tray.messageClicked.disconnect(self._tray_click_handler)
                except (RuntimeError, TypeError):
                    pass

            self._tray_click_handler = on_clicked
            tray.messageClicked.connect(on_clicked)
            tray.showMessage(
                title,
                message + "\n(Click to stop the looping alert and open the store page)",
                QSystemTrayIcon.MessageIcon.Information,
                10_000,
            )
        else:
            QMessageBox.information(None, title, message)


class SoundPlayer(QObject):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._player = QMediaPlayer()
        self._audio = QAudioOutput()
        self._player.setAudioOutput(self._audio)
        self._player.playbackStateChanged.connect(self._on_playback_state)
        self._sound_path: Path | None = None
        self._alert_active = False
        self._beep_timer = QTimer(self)
        self._beep_timer.setInterval(1000)
        self._beep_timer.timeout.connect(self._beep_once)

    @property
    def alert_active(self) -> bool:
        return self._alert_active

    def set_sound(self, path: str | Path | None) -> None:
        if path:
            resolved = Path(path)
            if resolved.is_file():
                self._sound_path = resolved
                return
        self._sound_path = default_sound_path()

    def start_alert_loop(self) -> None:
        self._alert_active = True
        self._beep_timer.stop()

        if self._sound_path and self._sound_path.is_file():
            self._player.setSource(QUrl.fromLocalFile(str(self._sound_path.resolve())))
            self._player.setLoops(QMediaPlayer.Loops.Infinite)
            self._player.play()
        else:
            self._beep_once()
            self._beep_timer.start()

    def stop(self) -> None:
        self._alert_active = False
        self._beep_timer.stop()
        self._player.stop()

    def _beep_once(self) -> None:
        if self._alert_active:
            QApplication.beep()

    @Slot(QMediaPlayer.PlaybackState)
    def _on_playback_state(self, state: QMediaPlayer.PlaybackState) -> None:
        if self._alert_active and state == QMediaPlayer.PlaybackState.StoppedState:
            self._player.play()


class NotificationBridge(QObject):
    """Marshals notification clicks from dbus worker thread to the Qt GUI thread."""

    clicked = Signal()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings(ORG_NAME, APP_NAME)
        self.notifications = NotificationService()
        self.sound = SoundPlayer(self)
        self._notify_bridge = NotificationBridge()
        self._notify_bridge.clicked.connect(self._on_notification_clicked)
        self.sound._player.playbackStateChanged.connect(self._sync_alert_ui)
        self._alert_url = STEAM_FRAME_URL

        self._last_frame_status = ProductStatus(
            self.settings.value(SETTINGS_LAST_STATUS, ProductStatus.UNKNOWN.value)
        )
        self._monitoring = self.settings.value(SETTINGS_MONITORING, True, type=bool)
        self._sound_path = self.settings.value(SETTINGS_SOUND, "")
        self.sound.set_sound(self._sound_path or None)

        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self._run_frame_check)

        self._build_ui()
        self._build_tray()
        self._sync_monitoring_ui()

        self.append_log("FrameMe started. Monitoring checks every 60 seconds when enabled.")
        if self._monitoring:
            self._timer.start()
            QTimer.singleShot(1500, self._run_frame_check)

    def _build_ui(self) -> None:
        self.setWindowTitle("FrameMe — Steam Frame Tracker")
        self.setMinimumSize(640, 520)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        header = QLabel(
            "<b>Steam Frame availability monitor</b><br>"
            "Runs in the system tray, checks every minute, and loops an alert until you "
            "click the notification to reserve."
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        status_row = QHBoxLayout()
        self.status_label = QLabel()
        status_row.addWidget(self.status_label, stretch=1)

        self.monitor_btn = QPushButton()
        self.monitor_btn.clicked.connect(self._toggle_monitoring)
        status_row.addWidget(self.monitor_btn)
        layout.addLayout(status_row)
        self._sync_monitoring_ui()

        sound_row = QHBoxLayout()
        sound_row.addWidget(QLabel("Alert sound:"))
        self.sound_field = QLineEdit()
        self.sound_field.setReadOnly(True)
        self.sound_field.setPlaceholderText("Default system sound")
        if self._sound_path:
            self.sound_field.setText(self._sound_path)
        sound_row.addWidget(self.sound_field, stretch=1)

        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_sound)
        sound_row.addWidget(browse_btn)

        clear_sound_btn = QPushButton("Reset")
        clear_sound_btn.clicked.connect(self._reset_sound)
        sound_row.addWidget(clear_sound_btn)
        layout.addLayout(sound_row)

        btn_row = QHBoxLayout()
        test_alert_btn = QPushButton("Test alert (loops until clicked)")
        test_alert_btn.clicked.connect(self._test_alert)
        btn_row.addWidget(test_alert_btn)

        self.stop_alert_btn = QPushButton("Stop alert")
        self.stop_alert_btn.setEnabled(False)
        self.stop_alert_btn.clicked.connect(self._stop_alert_only)
        btn_row.addWidget(self.stop_alert_btn)

        test_machine_btn = QPushButton("Test check (Steam Machine)")
        test_machine_btn.setToolTip(
            f"Runs a live API check against app {STEAM_MACHINE_APP_ID}. "
            "If this works, the Steam Frame check should work too."
        )
        test_machine_btn.clicked.connect(self._test_machine_check)
        btn_row.addWidget(test_machine_btn)

        check_now_btn = QPushButton("Check Steam Frame now")
        check_now_btn.clicked.connect(self._run_frame_check)
        btn_row.addWidget(check_now_btn)

        open_store_btn = QPushButton("Open store page")
        open_store_btn.clicked.connect(lambda: webbrowser.open(STEAM_FRAME_URL))
        btn_row.addWidget(open_store_btn)
        layout.addLayout(btn_row)

        layout.addWidget(QLabel("Log:"))
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.log_view, stretch=1)

        tray_hint = QLabel("Closing the window keeps FrameMe running in the tray.")
        tray_hint.setStyleSheet("color: gray;")
        layout.addWidget(tray_hint)

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self)
        self.tray.setToolTip("FrameMe — Steam Frame Tracker")

        icon = self.style().standardIcon(self.style().StandardPixmap.SP_ComputerIcon)
        self.tray.setIcon(icon)
        self.setWindowIcon(icon)

        menu = self.tray.contextMenu()
        if menu is None:
            from PySide6.QtWidgets import QMenu

            menu = QMenu()
            show_action = QAction("Show window", self)
            show_action.triggered.connect(self._show_window)
            menu.addAction(show_action)

            check_action = QAction("Check Steam Frame now", self)
            check_action.triggered.connect(self._run_frame_check)
            menu.addAction(check_action)

            menu.addSeparator()
            quit_action = QAction("Quit FrameMe", self)
            quit_action.triggered.connect(self._quit_app)
            menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _sync_monitoring_ui(self) -> None:
        if self._monitoring:
            self.monitor_btn.setText("Monitoring: ON")
            self.monitor_btn.setStyleSheet("font-weight: bold;")
            self.status_label.setText("Status: monitoring enabled (every 60s)")
        else:
            self.monitor_btn.setText("Monitoring: OFF")
            self.monitor_btn.setStyleSheet("")
            self.status_label.setText("Status: monitoring paused")

    def append_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log_view.append(f"[{stamp}] {message}")

    def _stop_monitoring(self, reason: str = "") -> None:
        was_active = self._monitoring
        self._monitoring = False
        self._timer.stop()
        self.settings.setValue(SETTINGS_MONITORING, False)
        self._sync_monitoring_ui()
        if reason and was_active:
            self.append_log(reason)

    @Slot()
    def _toggle_monitoring(self) -> None:
        if self._monitoring:
            self._stop_monitoring("Monitoring disabled.")
            return
        self._monitoring = True
        self.settings.setValue(SETTINGS_MONITORING, True)
        self._timer.start()
        self._sync_monitoring_ui()
        self.append_log("Monitoring enabled.")
        self._run_frame_check()

    @Slot()
    def _browse_sound(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select alert sound",
            str(Path.home()),
            "Audio files (*.wav *.mp3 *.ogg *.oga *.flac);;All files (*)",
        )
        if path:
            self._sound_path = path
            self.sound_field.setText(path)
            self.sound.set_sound(path)
            self.settings.setValue(SETTINGS_SOUND, path)
            self.append_log(f"Alert sound set to: {path}")

    @Slot()
    def _reset_sound(self) -> None:
        self._sound_path = ""
        self.sound_field.clear()
        self.sound.set_sound(None)
        self.settings.remove(SETTINGS_SOUND)
        self.append_log("Alert sound reset to default.")

    @Slot()
    def _test_alert(self) -> None:
        self.append_log("Test alert triggered — sound loops until you click the notification.")
        self._fire_alert(
            title="Steam Frame — test alert",
            message=(
                "Test alert: sound is looping. Click this notification to stop "
                "and open the Steam Frame reservation page."
            ),
        )

    def _sync_alert_ui(self) -> None:
        self.stop_alert_btn.setEnabled(self.sound.alert_active)

    @Slot()
    def _test_machine_check(self) -> None:
        self.append_log(f"Running test check for Steam Machine (app {STEAM_MACHINE_APP_ID})…")
        result = check_availability(STEAM_MACHINE_APP_ID)
        self._log_check_result(result, prefix="TEST")

    @Slot()
    def _run_frame_check(self) -> None:
        result = check_availability(STEAM_FRAME_APP_ID)
        self._log_check_result(result, prefix="CHECK")
        self._handle_frame_transition(result)

    def _log_check_result(self, result: CheckResult, prefix: str = "CHECK") -> None:
        if result.status is ProductStatus.ERROR:
            self.append_log(f"{prefix}: ERROR — {result.detail}")
            return
        self.append_log(f"{prefix}: {result.log_line}")

    def _handle_frame_transition(self, result: CheckResult) -> None:
        if result.status is ProductStatus.ERROR:
            return

        became_purchasable = (
            not self._last_frame_status.is_purchasable and result.status.is_purchasable
        )

        if became_purchasable:
            if result.status is ProductStatus.AVAILABLE:
                title = "Steam Frame is available!"
                message = (
                    "You can reserve a Steam Frame now! The alert is looping — "
                    "click this notification to stop it and open the store page."
                )
            else:
                title = "Steam Frame pre-orders open!"
                message = (
                    "Steam Frame pre-orders are open! The alert is looping — "
                    "click this notification to stop it and reserve yours."
                )
            self.append_log(f"ALERT: {result.status.label()} — sending notification.")
            self._fire_alert(title=title, message=message)
            self._stop_monitoring(
                "Monitoring stopped automatically — Steam Frame is ready to reserve."
            )

        self._last_frame_status = result.status
        self.settings.setValue(SETTINGS_LAST_STATUS, result.status.value)

    def _fire_alert(self, title: str, message: str) -> None:
        self._alert_url = STEAM_FRAME_URL
        self.sound.start_alert_loop()
        self._sync_alert_ui()
        self.notifications.send(
            title,
            message,
            self.tray,
            on_clicked=self._notify_bridge.clicked.emit,
        )
        if not self.isVisible():
            self.tray.showMessage(APP_NAME, message, QSystemTrayIcon.MessageIcon.Information, 5000)

    @Slot()
    def _stop_alert_only(self) -> None:
        if self.sound.alert_active:
            self.sound.stop()
            self._sync_alert_ui()
            self.append_log("Alert stopped manually.")

    @Slot()
    def _on_notification_clicked(self) -> None:
        self.sound.stop()
        self._sync_alert_ui()
        webbrowser.open(self._alert_url)

    @Slot()
    def _show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    @Slot(QSystemTrayIcon.ActivationReason)
    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._show_window()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.tray.isVisible():
            event.ignore()
            self.hide()
            self.tray.showMessage(
                APP_NAME,
                "Still running in the system tray.",
                QSystemTrayIcon.MessageIcon.Information,
                2500,
            )
        else:
            super().closeEvent(event)

    def _quit_app(self) -> None:
        self.sound.stop()
        self._timer.stop()
        self.tray.hide()
        QApplication.quit()


def main() -> int:
    QApplication.setOrganizationName(ORG_NAME)
    QApplication.setApplicationName(APP_NAME)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(
            None,
            APP_NAME,
            "System tray is not available on this desktop environment.",
        )
        return 1

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
