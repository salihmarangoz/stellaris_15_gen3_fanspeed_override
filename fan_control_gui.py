import sys
import time
import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QStyle,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from fan_control_service import ControlCenterService
from temperature_service import Temperatures, read_temperatures


INSTANCE_SERVER_NAME = "blabla.fan-control"
AUTO_INTERVAL_MS = 15000
AUTO_CURVE = (
    (40, 30),
    (50, 40),
    (60, 55),
    (70, 75),
    (80, 100),
)


class WorkerSignals(QObject):
    completed = Signal(object)
    failed = Signal(str)


class Worker(QRunnable):
    def __init__(self, operation: Callable[[], Any]) -> None:
        super().__init__()
        self.operation = operation
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            result = self.operation()
        except Exception:
            try:
                self.signals.failed.emit(traceback.format_exc())
            except RuntimeError:
                pass
            return
        try:
            self.signals.completed.emit(result)
        except RuntimeError:
            pass


class FanControlWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Fan Control")
        self.setMinimumSize(600, 520)
        self.resize(660, 560)

        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._service = ControlCenterService.instance()
        self._busy = False
        self._telemetry_inflight = False
        self._closing = False
        self._syncing = False
        self._dirty = False
        self._auto_inflight = False
        self._auto_backup_needed = True
        self._table_name = ""
        self._status_message = "Connecting to Control Center..."
        self._status_updated_at: float | None = None
        self._workers: set[Worker] = set()

        self._build_ui()
        self._apply_style()

        self._telemetry_timer = QTimer(self)
        self._telemetry_timer.setInterval(10000)
        self._telemetry_timer.timeout.connect(self.refresh_telemetry)
        self._telemetry_timer.start()

        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(AUTO_INTERVAL_MS)
        self._auto_timer.timeout.connect(self.run_auto_cycle)

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._refresh_status_age)
        self._status_timer.start()
        QTimer.singleShot(0, self.load_state)

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(18)

        heading_row = QHBoxLayout()
        heading = QLabel("Fan Control")
        heading.setObjectName("heading")
        heading_row.addWidget(heading)
        heading_row.addStretch()

        self.refresh_button = QPushButton()
        self.refresh_button.setObjectName("iconButton")
        self.refresh_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self.refresh_button.setToolTip("Refresh fan state")
        self.refresh_button.clicked.connect(self.load_state)
        heading_row.addWidget(self.refresh_button)
        layout.addLayout(heading_row)

        self.connection_label = QLabel(self._status_message)
        self.connection_label.setObjectName("statusText")
        layout.addWidget(self.connection_label)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setObjectName("divider")
        layout.addWidget(divider)

        self.tabs = QTabWidget()
        self.manual_tab = QWidget()
        self.auto_tab = QWidget()
        self.tabs.addTab(self.manual_tab, "Manual")
        self.tabs.addTab(self.auto_tab, "Auto")
        self.tabs.currentChanged.connect(self._mode_changed)
        layout.addWidget(self.tabs, 1)

        manual_layout = QVBoxLayout(self.manual_tab)
        manual_layout.setContentsMargins(4, 18, 4, 4)
        manual_layout.setSpacing(18)
        self.cpu_slider, self.cpu_spin, self.cpu_reported = self._fan_row(
            manual_layout, "CPU fan"
        )
        self.gpu_slider, self.gpu_spin, self.gpu_reported = self._fan_row(
            manual_layout, "GPU fan"
        )

        warning = QLabel("Values below 30% may stop a fan and require confirmation.")
        warning.setObjectName("warningText")
        warning.setWordWrap(True)
        manual_layout.addWidget(warning)
        manual_layout.addStretch()

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.boost_button = QPushButton("Fan Boost 100%")
        self.boost_button.setCheckable(True)
        self.boost_button.setToolTip("Toggle the OEM 100% fan override")
        self.boost_button.toggled.connect(self.toggle_boost)
        actions.addWidget(self.boost_button)
        actions.addStretch()

        self.apply_button = QPushButton("Apply manual speeds")
        self.apply_button.setObjectName("primaryButton")
        self.apply_button.clicked.connect(self.apply_speeds)
        actions.addWidget(self.apply_button)
        manual_layout.addLayout(actions)

        auto_layout = QVBoxLayout(self.auto_tab)
        auto_layout.setContentsMargins(4, 18, 4, 4)
        auto_layout.setSpacing(14)
        self.cpu_temp_label = QLabel("CPU temperature: --.- C")
        self.cpu_temp_label.setObjectName("temperatureValue")
        self.cpu_auto_label = QLabel("Target: --%")
        self.cpu_auto_label.setObjectName("reportedValue")
        auto_layout.addWidget(self.cpu_temp_label)
        auto_layout.addWidget(self.cpu_auto_label)

        self.gpu_temp_label = QLabel("GPU temperature: --.- C")
        self.gpu_temp_label.setObjectName("temperatureValue")
        self.gpu_auto_label = QLabel("Target: --%")
        self.gpu_auto_label.setObjectName("reportedValue")
        auto_layout.addWidget(self.gpu_temp_label)
        auto_layout.addWidget(self.gpu_auto_label)

        curve_title = QLabel("Automatic curve")
        curve_title.setObjectName("fanName")
        auto_layout.addWidget(curve_title)
        curve_text = "   ".join(f"{temp} C: {duty}%" for temp, duty in AUTO_CURVE)
        curve_label = QLabel(curve_text)
        curve_label.setWordWrap(True)
        curve_label.setObjectName("statusText")
        auto_layout.addWidget(curve_label)
        self.source_label = QLabel("CPU: PawnIO Ryzen SMN | GPU: NVIDIA driver")
        self.source_label.setWordWrap(True)
        self.source_label.setObjectName("statusText")
        auto_layout.addWidget(self.source_label)
        auto_layout.addStretch()

    def _fan_row(
        self, parent_layout: QVBoxLayout, title: str
    ) -> tuple[QSlider, QSpinBox, QLabel]:
        labels = QHBoxLayout()
        name = QLabel(title)
        name.setObjectName("fanName")
        labels.addWidget(name)
        labels.addStretch()
        reported = QLabel("Reported: --%")
        reported.setObjectName("reportedValue")
        labels.addWidget(reported)
        parent_layout.addLayout(labels)

        controls = QHBoxLayout()
        controls.setSpacing(14)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        slider.setSingleStep(5)
        slider.setPageStep(5)
        slider.setTickInterval(5)
        slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        slider.setValue(50)
        controls.addWidget(slider, 1)

        spin = QSpinBox()
        spin.setRange(0, 100)
        spin.setSingleStep(5)
        spin.setSuffix(" %")
        spin.setFixedWidth(88)
        spin.setValue(50)
        controls.addWidget(spin)
        parent_layout.addLayout(controls)

        slider.valueChanged.connect(spin.setValue)
        spin.valueChanged.connect(slider.setValue)
        slider.valueChanged.connect(self._mark_dirty)
        slider.sliderReleased.connect(
            lambda: slider.setValue(((slider.value() + 2) // 5) * 5)
        )
        spin.editingFinished.connect(
            lambda: spin.setValue(((spin.value() + 2) // 5) * 5)
        )
        return slider, spin, reported

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #17191c;
                color: #eef1f3;
            }
            QLabel#heading {
                font-size: 24px;
                font-weight: 650;
            }
            QLabel#statusText, QLabel#reportedValue {
                color: #aeb6bd;
            }
            QLabel#fanName {
                font-size: 15px;
                font-weight: 600;
            }
            QLabel#temperatureValue {
                font-size: 19px;
                font-weight: 650;
            }
            QLabel#warningText {
                color: #e4b45d;
                font-size: 12px;
            }
            QFrame#divider {
                color: #34393e;
            }
            QSlider {
                min-height: 34px;
            }
            QSlider::groove:horizontal {
                height: 6px;
                background: #34393e;
                border-radius: 3px;
            }
            QSlider::sub-page:horizontal {
                background: #25b99a;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                width: 18px;
                margin: -6px 0;
                background: #f4f7f8;
                border: 2px solid #25b99a;
                border-radius: 9px;
            }
            QSpinBox {
                min-height: 34px;
                padding: 0 8px;
                background: #22262a;
                border: 1px solid #444b51;
                border-radius: 5px;
            }
            QPushButton {
                min-height: 36px;
                padding: 0 14px;
                background: #292e33;
                border: 1px solid #454c52;
                border-radius: 5px;
            }
            QPushButton:hover {
                background: #343a40;
            }
            QPushButton:checked {
                color: #17191c;
                background: #e4b45d;
                border-color: #e4b45d;
            }
            QPushButton#primaryButton {
                color: #07130f;
                background: #25b99a;
                border-color: #25b99a;
                font-weight: 650;
            }
            QPushButton#primaryButton:hover {
                background: #34c8a8;
            }
            QPushButton#iconButton {
                min-width: 38px;
                max-width: 38px;
                padding: 0;
            }
            QPushButton:disabled, QSpinBox:disabled, QSlider:disabled {
                color: #727980;
                background: #24272a;
            }
            QTabWidget::pane {
                border: 1px solid #34393e;
                border-radius: 5px;
            }
            QTabBar::tab {
                min-width: 100px;
                min-height: 34px;
                padding: 0 12px;
                background: #22262a;
                border: 1px solid #34393e;
            }
            QTabBar::tab:selected {
                color: #07130f;
                background: #25b99a;
                border-color: #25b99a;
            }
            """
        )

    def _mark_dirty(self) -> None:
        if not self._syncing:
            self._dirty = True

    def _set_busy(self, busy: bool, message: str | None = None) -> None:
        self._busy = busy
        self.refresh_button.setEnabled(not busy)
        self.apply_button.setEnabled(not busy)
        self.boost_button.setEnabled(not busy)
        if message:
            self._set_status(message)

    def _set_status(self, message: str, *, updated: bool = False) -> None:
        self._status_message = message
        self._status_updated_at = time.monotonic() if updated else None
        self._refresh_status_age()

    def _refresh_status_age(self) -> None:
        if self._status_updated_at is None:
            self.connection_label.setText(self._status_message)
            return
        seconds = max(0, int(time.monotonic() - self._status_updated_at))
        unit = "second" if seconds == 1 else "seconds"
        self.connection_label.setText(
            f"{self._status_message} (last updated {seconds} {unit} ago)"
        )

    def _mode_changed(self, index: int) -> None:
        if self.tabs.widget(index) is self.auto_tab:
            self._auto_backup_needed = True
            self._auto_timer.start()
            QTimer.singleShot(0, self.run_auto_cycle)
        else:
            self._auto_timer.stop()
            self._set_status("Manual mode | Apply speeds when ready")

    def _run(
        self,
        operation: Callable[[], Any],
        on_complete: Callable[[Any], None],
        busy_message: str,
    ) -> None:
        if self._busy or self._closing:
            return
        self._set_busy(True, busy_message)
        worker = Worker(operation)
        self._workers.add(worker)

        def complete(result: Any) -> None:
            self._workers.discard(worker)
            if self._closing:
                return
            self._set_busy(False)
            on_complete(result)

        def failed(details: str) -> None:
            self._workers.discard(worker)
            if self._closing:
                return
            self._set_busy(False, "Control Center communication failed")
            QMessageBox.critical(self, "Fan control error", details)

        worker.signals.completed.connect(complete)
        worker.signals.failed.connect(failed)
        self._pool.start(worker)

    def load_state(self) -> None:
        self._run(self._service.load_state, self._show_state, "Reading fan state...")

    def _show_state(self, result: dict[str, Any]) -> None:
        status = result["status"]
        curve = result["curve"]
        self._table_name = str(status["FAN_TableName"])
        self._syncing = True
        try:
            self.cpu_slider.setValue(int(curve["CPU"][0]["Duty"]))
            self.gpu_slider.setValue(int(curve["GPU"][0]["Duty"]))
            self.boost_button.blockSignals(True)
            self.boost_button.setChecked(str(status["FanBoostEnable"]) == "1")
            self.boost_button.blockSignals(False)
        finally:
            self._syncing = False
        self._dirty = False
        self._show_telemetry(result["telemetry"])

    def refresh_telemetry(self) -> None:
        if self._busy or self._telemetry_inflight or self._auto_inflight or self._closing:
            return
        self._telemetry_inflight = True
        worker = Worker(self._service.read_telemetry)
        self._workers.add(worker)

        def complete(result: dict[str, Any]) -> None:
            self._workers.discard(worker)
            self._telemetry_inflight = False
            if not self._closing:
                self._show_telemetry(result)

        def failed(details: str) -> None:
            self._workers.discard(worker)
            self._telemetry_inflight = False
            if not self._closing:
                self._set_status("Connected | Telemetry temporarily unavailable")

        worker.signals.completed.connect(complete)
        worker.signals.failed.connect(failed)
        self._pool.start(worker)

    def _show_telemetry(self, telemetry: dict[str, Any]) -> None:
        self.cpu_reported.setText(f"Reported: {telemetry.get('CpuFanDuty', '--')}%")
        self.gpu_reported.setText(f"Reported: {telemetry.get('GpuFanDuty', '--')}%")
        if self._table_name and self.tabs.currentWidget() is self.manual_tab:
            self._set_status(
                f"Connected | Active table: {self._table_name}", updated=True
            )

    def _confirm_low_values(self, cpu: int, gpu: int) -> bool:
        low_values = []
        if cpu < 30:
            low_values.append(f"CPU fan: {cpu}%")
        if gpu < 30:
            low_values.append(f"GPU fan: {gpu}%")
        if not low_values:
            return True

        message = (
            "A fan duty below 30% may be too low to start or keep the fan spinning.\n\n"
            + "\n".join(low_values)
            + "\n\nApply these values anyway?"
        )
        answer = QMessageBox.warning(
            self,
            "Confirm low fan duty",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def apply_speeds(self) -> None:
        cpu = self.cpu_spin.value()
        gpu = self.gpu_spin.value()
        if not self._confirm_low_values(cpu, gpu):
            return

        def complete(result: dict[str, Any]) -> None:
            self._dirty = False
            self.boost_button.blockSignals(True)
            self.boost_button.setChecked(False)
            self.boost_button.blockSignals(False)
            self._set_status(
                f"Applied CPU {result['cpu']}% | GPU {result['gpu']}% | Ramping..."
            )
            QTimer.singleShot(4000, self.refresh_telemetry)

        self._run(
            lambda: self._service.apply_manual(cpu, gpu),
            complete,
            "Applying manual fan speeds...",
        )

    @staticmethod
    def _auto_target(temperature: float) -> int:
        if temperature <= AUTO_CURVE[0][0]:
            return AUTO_CURVE[0][1]
        for (low_temp, low_duty), (high_temp, high_duty) in zip(
            AUTO_CURVE, AUTO_CURVE[1:]
        ):
            if temperature <= high_temp:
                position = (temperature - low_temp) / (high_temp - low_temp)
                duty = low_duty + position * (high_duty - low_duty)
                return max(30, min(100, round(duty / 5) * 5))
        return AUTO_CURVE[-1][1]

    def run_auto_cycle(self) -> None:
        if (
            self.tabs.currentWidget() is not self.auto_tab
            or self._busy
            or self._telemetry_inflight
            or self._auto_inflight
            or self._closing
        ):
            return

        self._auto_inflight = True
        make_backup = self._auto_backup_needed

        def operation() -> dict[str, Any]:
            temperatures = read_temperatures()
            hottest = max(temperatures.cpu_c, temperatures.gpu_c)
            target = self._auto_target(hottest)
            applied = self._service.apply_manual(
                target, target, create_backup=make_backup
            )
            return {
                "temperatures": temperatures,
                "hottest": hottest,
                "applied": applied,
            }

        worker = Worker(operation)
        self._workers.add(worker)

        def complete(result: dict[str, Any]) -> None:
            self._workers.discard(worker)
            self._auto_inflight = False
            if self._closing:
                return
            self._auto_backup_needed = False
            temperatures: Temperatures = result["temperatures"]
            applied = result["applied"]
            self.cpu_temp_label.setText(
                f"CPU temperature: {temperatures.cpu_c:.1f} C"
            )
            self.gpu_temp_label.setText(
                f"GPU temperature: {temperatures.gpu_c:.1f} C"
            )
            self.cpu_auto_label.setText(f"Target: {applied['cpu']}%")
            self.gpu_auto_label.setText(f"Target: {applied['gpu']}%")
            self.source_label.setText(
                f"CPU: {temperatures.cpu_source} | GPU: {temperatures.gpu_source}"
            )
            self._set_status(
                f"Auto mode | Max temperature {result['hottest']:.1f} C",
                updated=True,
            )

        def failed(details: str) -> None:
            self._workers.discard(worker)
            self._auto_inflight = False
            if not self._closing:
                last_line = details.strip().splitlines()[-1]
                self._set_status(f"Auto mode error | {last_line}")

        worker.signals.completed.connect(complete)
        worker.signals.failed.connect(failed)
        self._pool.start(worker)

    def toggle_boost(self, enabled: bool) -> None:
        def complete(state: bool) -> None:
            self._set_status(
                "Fan Boost enabled" if state else "Manual fan control restored"
            )
            QTimer.singleShot(1500, self.refresh_telemetry)

        self._run(
            lambda: self._service.set_boost(enabled),
            complete,
            "Enabling Fan Boost..." if enabled else "Disabling Fan Boost...",
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        self._telemetry_timer.stop()
        self._auto_timer.stop()
        self._status_timer.stop()
        self._service.close(wait=False)
        event.accept()

    def activate_from_second_instance(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()


def notify_existing_instance() -> bool:
    socket = QLocalSocket()
    socket.connectToServer(INSTANCE_SERVER_NAME)
    if not socket.waitForConnected(300):
        return False
    socket.write(b"ACTIVATE")
    socket.waitForBytesWritten(300)
    socket.disconnectFromServer()
    return True


def create_instance_server(window: FanControlWindow) -> QLocalServer:
    QLocalServer.removeServer(INSTANCE_SERVER_NAME)
    server = QLocalServer(window)
    if not server.listen(INSTANCE_SERVER_NAME):
        raise RuntimeError(f"Could not create instance server: {server.errorString()}")

    def accept_connection() -> None:
        while server.hasPendingConnections():
            socket = server.nextPendingConnection()
            socket.waitForReadyRead(100)
            socket.readAll()
            window.activate_from_second_instance()
            socket.disconnectFromServer()

    server.newConnection.connect(accept_connection)
    return server


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Fan Control")
    if notify_existing_instance():
        return
    window = FanControlWindow()
    window._instance_server = create_instance_server(window)
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
