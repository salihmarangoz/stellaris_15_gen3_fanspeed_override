import json
import os
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRectF, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QCloseEvent, QDesktopServices, QIcon, QPainter, QPen
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from shared.fan_control_common import (
    DEFAULT_MAX_FAN_TEMP,
    DEFAULT_MIN_FAN_TEMP,
    MAX_AUTO_DUTY,
    MIN_AUTO_DUTY,
    auto_target,
)
from shared.fan_control_ipc import (
    BackendClient,
    LINUX_SERVICE_NAME,
    RESTART_COOLDOWN_SECONDS,
    ensure_backend,
    is_linux,
    launch_component,
)


INSTANCE_SERVER_NAME = "stellaris15gen3.fan-control"
STYLESHEET_NAME = "stellaris15gen3.css"
ICON_FILENAME = "stellaris-fan-control.png"
SETTINGS_FILENAME = "StellarisFanControl.json"
PROJECT_URL = "https://github.com/salihmarangoz/stellaris_15_gen3_fanspeed_override"
LINUX_CONFIG_DIRECTORY = "stellaris-fan-control"
GPU_POWER_STEP_WATTS = 5
CPU_POWER_GAUGE_MAX_WATTS = 60
GPU_POWER_GAUGE_MAX_WATTS = 165
# NVIDIA applied cTGP and Dynamic Boost changes within 1.5 s in the live test.
GPU_LIMIT_SETTLE_SECONDS = 1.5
GPU_LIMIT_WAIT_SECONDS = 12.0
GPU_LIMIT_REFRESH_DELAYS_MS = (2000, 4000, 7000)
DEFAULT_GPU_POWER_MAX_OFFSET = 50
SERVICE_START_HINT = f"start it with 'sudo systemctl start {LINUX_SERVICE_NAME}'"


class FanCurveGraph(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._minimum_temp = DEFAULT_MIN_FAN_TEMP
        self._maximum_temp = DEFAULT_MAX_FAN_TEMP
        self.setMinimumHeight(150)

    def set_temperatures(self, minimum_temp: int, maximum_temp: int) -> None:
        self._minimum_temp = minimum_temp
        self._maximum_temp = maximum_temp
        self.update()

    def paintEvent(self, event: object) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        left, top, right, bottom = 38, 12, 14, 28
        width = max(1, self.width() - left - right)
        height = max(1, self.height() - top - bottom)

        grid_pen = QPen(QColor("#34393e"), 1)
        painter.setPen(grid_pen)
        for value in range(0, 101, 20):
            x = left + width * value / 100
            y = top + height * (100 - value) / 100
            painter.drawLine(int(x), top, int(x), top + height)
            painter.drawLine(left, int(y), left + width, int(y))

        label_pen = QPen(QColor("#aeb6bd"), 1)
        painter.setPen(label_pen)
        painter.drawText(2, top + 5, "100%")
        painter.drawText(10, top + height + 5, "0%")
        painter.drawText(left - 4, self.height() - 5, "0 C")
        painter.drawText(left + width - 28, self.height() - 5, "100 C")

        def point(temp: int, duty: int) -> tuple[int, int]:
            return (
                int(left + width * temp / 100),
                int(top + height * (100 - duty) / 100),
            )

        curve_color = QColor("#25b99a" if self.isEnabled() else "#727980")
        curve_pen = QPen(curve_color, 3)
        painter.setPen(curve_pen)
        points = [
            point(
                temperature,
                auto_target(
                    temperature, self._minimum_temp, self._maximum_temp
                ),
            )
            for temperature in range(101)
        ]
        for start, end in zip(points, points[1:]):
            painter.drawLine(*start, *end)

        painter.setBrush(curve_color)
        marker_temperatures = {
            self._minimum_temp,
            min(self._maximum_temp, 80),
        }
        for temperature in marker_temperatures:
            x, y = points[temperature]
            painter.drawEllipse(x - 4, y - 4, 8, 8)


class SensorGauge(QWidget):
    def __init__(
        self,
        title: str,
        unit: str,
        *,
        temperature_colors: bool,
        maximum: float = 100.0,
        decimals: int | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._unit = unit
        self._temperature_colors = temperature_colors
        self._maximum = maximum
        self._decimals = (1 if temperature_colors else 0) if decimals is None else decimals
        self._value: float | None = None
        self._placeholder = "--"
        self._detail = ""
        self.setMinimumSize(110, 112)
        self.setAccessibleName(f"{title} gauge")

    def set_maximum(self, maximum: float) -> None:
        if maximum > 0 and maximum != self._maximum:
            self._maximum = maximum
            self.update()

    def set_value(
        self, value: float | None, *, placeholder: str = "--", detail: str = ""
    ) -> None:
        self._value = value
        self._placeholder = placeholder
        self._detail = detail
        if value is None:
            self.setAccessibleDescription(
                "Value unavailable" if placeholder == "--" else placeholder
            )
        else:
            self.setAccessibleDescription(
                f"{value:.{self._decimals}f} {self._unit}" + (f", {detail}" if detail else "")
            )
        self.update()

    def paintEvent(self, event: object) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = float(self.width())
        title_height = 20.0
        scale_height = 14.0
        # Every gauge reserves the detail line so all gauges share one size.
        detail_height = 16.0
        available = self.height() - title_height - 6 - scale_height - detail_height
        diameter = max(40.0, min(width - 24.0, 2.0 * available, 150.0))
        top = title_height + 6
        arc_rect = QRectF((width - diameter) / 2, top, diameter, diameter)
        center_y = top + diameter / 2

        title_font = painter.font()
        title_font.setBold(True)
        title_font.setPointSize(11)
        painter.setFont(title_font)
        painter.setPen(QColor("#eef1f3"))
        painter.drawText(
            QRectF(0, 0, width, title_height),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            self._title,
        )

        pen_width = max(7.0, diameter * 0.075)
        background_pen = QPen(QColor("#34393e"), pen_width)
        background_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(background_pen)
        painter.drawArc(arc_rect, 180 * 16, -180 * 16)

        if self._value is None:
            gauge_color = QColor("#727980")
            span = 0
            value_text = (
                f"-- {self._unit}" if self._placeholder == "--" else self._placeholder
            )
        else:
            fraction = max(0.0, min(1.0, self._value / self._maximum))
            if self._temperature_colors and self._value >= 80:
                gauge_color = QColor("#e35d6a")
            elif self._temperature_colors and self._value >= 60:
                gauge_color = QColor("#e4b45d")
            else:
                gauge_color = QColor("#25b99a")
            span = round(-180 * 16 * fraction)
            value_text = f"{self._value:.{self._decimals}f} {self._unit}"

        value_pen = QPen(gauge_color, pen_width)
        value_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(value_pen)
        painter.drawArc(arc_rect, 180 * 16, span)

        value_font = painter.font()
        value_font.setBold(True)
        value_font.setPointSize(14 if diameter >= 110 else 12)
        painter.setFont(value_font)
        painter.setPen(gauge_color)
        painter.drawText(
            QRectF(0, center_y - 32, width, 28),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
            value_text,
        )

        scale_font = painter.font()
        scale_font.setBold(False)
        scale_font.setPointSize(8)
        painter.setFont(scale_font)
        painter.setPen(QColor("#aeb6bd"))
        scale_rect = QRectF(arc_rect.left() - 8, center_y, diameter + 16, scale_height)
        painter.drawText(scale_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "0")
        painter.drawText(
            scale_rect,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"{self._maximum:g}",
        )

        if self._detail:
            detail_font = painter.font()
            detail_font.setPointSize(10)
            painter.setFont(detail_font)
            painter.drawText(
                QRectF(0, center_y + scale_height, width, detail_height),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                self._detail,
            )


class DisabledPanelOverlay(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("disabledPanelOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event: object) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(115, 120, 125, 105))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 7, 7)


class ControlPanel(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self._disabled_overlay = DisabledPanelOverlay(self)
        self._disabled_overlay.hide()

    def setEnabled(self, enabled: bool) -> None:
        super().setEnabled(enabled)
        self._disabled_overlay.setVisible(not enabled)
        if not enabled:
            self._disabled_overlay.raise_()

    def resizeEvent(self, event: object) -> None:
        self._disabled_overlay.setGeometry(self.rect())
        self._disabled_overlay.raise_()
        super().resizeEvent(event)


class ModeToggle(QAbstractButton):
    def __init__(self) -> None:
        super().__init__()
        self.setCheckable(True)
        self.setChecked(False)
        self.setFixedSize(220, 40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("Control mode")
        self.toggled.connect(self._update_accessibility)
        self._update_accessibility(self.isChecked())

    def is_manual(self) -> bool:
        return self.isChecked()

    def nextCheckState(self) -> None:
        target = "Automatic" if self.is_manual() else "Manual"
        explanation = (
            "Automatic mode will immediately adjust both fans using CPU and GPU temperatures."
            if self.is_manual()
            else "Automatic adjustments will stop. You will control the fan targets manually."
        )
        answer = QMessageBox.question(
            self,
            "Confirm mode change",
            f"Switch to {target} mode?\n\n{explanation}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            super().nextCheckState()

    def _update_accessibility(self, manual: bool) -> None:
        self.setAccessibleDescription("Manual mode" if manual else "Automatic mode")
        self.update()

    def paintEvent(self, event: object) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        outer = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor("#454c52"), 1))
        painter.setBrush(QColor("#292e33"))
        painter.drawRoundedRect(outer, 7, 7)

        half_width = outer.width() / 2
        active = QRectF(
            outer.left() + (half_width if self.is_manual() else 0),
            outer.top(),
            half_width,
            outer.height(),
        )
        active_color = QColor("#25b99a" if self.isEnabled() else "#727980")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(active_color)
        painter.drawRoundedRect(active, 6, 6)

        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        automatic_rect = QRectF(outer.left(), outer.top(), half_width, outer.height())
        manual_rect = QRectF(
            outer.left() + half_width, outer.top(), half_width, outer.height()
        )
        painter.setPen(QColor("#07130f") if not self.is_manual() else QColor("#eef1f3"))
        painter.drawText(automatic_rect, Qt.AlignmentFlag.AlignCenter, "Automatic")
        painter.setPen(QColor("#07130f") if self.is_manual() else QColor("#eef1f3"))
        painter.drawText(manual_rect, Qt.AlignmentFlag.AlignCenter, "Manual")


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
    def __init__(self, backend: Any | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Fan Control")
        self.setMinimumSize(1080, 760)
        self.resize(1240, 800)
        self._linux = is_linux()

        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._backend = backend if backend is not None else BackendClient()
        self._busy = False
        self._telemetry_inflight = False
        self._backend_check_inflight = False
        self._backend_offline = False
        self._last_backend_restart = 0.0
        self._closing = False
        self._exit_in_progress = False
        self._exit_prepared = False
        self._syncing = False
        self._curve_syncing = False
        self._last_manual_values = (50, 50)
        self._preferences = self._load_preferences()
        self._table_name = ""
        self._control_method: str | None = None
        self._status_message = "Connecting to Control Center..."
        self._status_updated_at: float | None = None
        self._workers: set[Worker] = set()

        self._manual_apply_timer = QTimer(self)
        self._manual_apply_timer.setSingleShot(True)
        self._manual_apply_timer.setInterval(300)
        self._manual_apply_timer.timeout.connect(self.apply_speeds)
        self._gpu_power_apply_timer = QTimer(self)
        self._gpu_power_apply_timer.setSingleShot(True)
        self._gpu_power_apply_timer.setInterval(400)
        self._gpu_power_apply_timer.timeout.connect(self.apply_gpu_power)
        self._gpu_power_available = False
        self._gpu_power_base: float | None = None
        self._gpu_power_state: dict[str, Any] = {}
        self._faults: dict[str, str | None] = {}
        self._gpu_power_state_at = 0.0
        self._gpu_power_changed_at: float | None = None
        self._gpu_power_applied: int | None = None
        self._state_loaded = False
        self._last_state_attempt = 0.0

        self._build_ui()
        self._apply_preferences_to_controls()
        self._apply_style()
        self._setup_tray()

        self._telemetry_timer = QTimer(self)
        self._telemetry_timer.setInterval(10000)
        self._telemetry_timer.timeout.connect(self.refresh_telemetry)
        self._telemetry_timer.start()

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._refresh_status_age)
        self._status_timer.start()

        self._backend_watchdog_timer = QTimer(self)
        self._backend_watchdog_timer.setInterval(5000)
        self._backend_watchdog_timer.timeout.connect(self.check_backend)
        self._backend_watchdog_timer.start()
        QTimer.singleShot(0, self._load_initial_state)

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 18, 28, 18)
        layout.setSpacing(12)

        heading_row = QHBoxLayout()
        heading = QLabel("Fan Control")
        heading.setObjectName("heading")
        heading_row.addWidget(heading)
        heading_row.addStretch()
        self.control_method_label = QLabel("Detecting control...")
        self.control_method_label.setObjectName("methodBadge")
        heading_row.addWidget(self.control_method_label)
        self.exit_button = QPushButton("Quit" if self._linux else "Exit")
        self.exit_button.setObjectName("exitButton")
        self.exit_button.setToolTip(
            "Close this window and the tray icon. The fan-control service keeps running."
            if self._linux
            else "Set both fans to 100% and exit"
        )
        self.exit_button.clicked.connect(self.request_exit)
        heading_row.addWidget(self.exit_button)
        layout.addLayout(heading_row)

        self.connection_label = QLabel(self._status_message)
        self.connection_label.setObjectName("statusText")
        layout.addWidget(self.connection_label)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setObjectName("divider")
        layout.addWidget(divider)

        mode_row = QHBoxLayout()
        mode_label = QLabel("Control mode")
        mode_label.setObjectName("fanName")
        mode_row.addWidget(mode_label)
        mode_row.addSpacing(10)
        self.mode_toggle = ModeToggle()
        self.mode_toggle.toggled.connect(self._mode_changed)
        mode_row.addWidget(self.mode_toggle)
        mode_row.addStretch()
        self.start_minimized_checkbox = QCheckBox("Start minimized")
        self.start_minimized_checkbox.setObjectName("startMinimized")
        self.start_minimized_checkbox.setToolTip(
            "Start in the system tray on the next launch. Fan control stays active."
        )
        self.start_minimized_checkbox.toggled.connect(self._start_minimized_toggled)
        mode_row.addWidget(self.start_minimized_checkbox)
        layout.addLayout(mode_row)

        columns = QHBoxLayout()
        columns.setSpacing(14)
        layout.addLayout(columns, 1)

        self.auto_panel = ControlPanel()
        self.auto_panel.setObjectName("panel")
        auto_layout = QVBoxLayout(self.auto_panel)
        auto_layout.setContentsMargins(18, 14, 18, 14)
        auto_layout.setSpacing(8)
        auto_title = QLabel("Automatic")
        auto_title.setObjectName("sectionTitle")
        auto_layout.addWidget(auto_title)
        auto_help = QLabel(
            "Both fans follow the hottest sensor. The target rises from 30% to "
            "100% between these temperatures. The 80 C safety cap always forces 100%."
        )
        auto_help.setWordWrap(True)
        auto_help.setObjectName("statusText")
        auto_layout.addWidget(auto_help)
        self.min_temp_slider, self.min_temp_spin = self._temperature_row(
            auto_layout, "30% fan speed at", DEFAULT_MIN_FAN_TEMP
        )
        self.max_temp_slider, self.max_temp_spin = self._temperature_row(
            auto_layout, "100% fan speed at", DEFAULT_MAX_FAN_TEMP
        )
        self.min_temp_slider.valueChanged.connect(self._minimum_temp_changed)
        self.min_temp_spin.valueChanged.connect(self._minimum_temp_changed)
        self.max_temp_slider.valueChanged.connect(self._maximum_temp_changed)
        self.max_temp_spin.valueChanged.connect(self._maximum_temp_changed)
        self.min_temp_slider.sliderReleased.connect(self._configure_auto)
        self.min_temp_spin.editingFinished.connect(self._configure_auto)
        self.max_temp_slider.sliderReleased.connect(self._configure_auto)
        self.max_temp_spin.editingFinished.connect(self._configure_auto)

        reset_row = QHBoxLayout()
        reset_row.addStretch()
        self.reset_curve_button = QPushButton("Reset to 35 / 75 C")
        self.reset_curve_button.clicked.connect(self._reset_auto_temperatures)
        reset_row.addWidget(self.reset_curve_button)
        auto_layout.addLayout(reset_row)
        self.curve_graph = FanCurveGraph()
        auto_layout.addWidget(self.curve_graph, 1)
        self.auto_target_label = QLabel("Shared target: --%")
        self.auto_target_label.setObjectName("reportedValue")
        auto_layout.addWidget(self.auto_target_label)
        columns.addWidget(self.auto_panel, 1)

        self.manual_panel = ControlPanel()
        self.manual_panel.setObjectName("panel")
        manual_layout = QVBoxLayout(self.manual_panel)
        manual_layout.setContentsMargins(18, 14, 18, 14)
        manual_layout.setSpacing(14)
        manual_title = QLabel("Manual control")
        manual_title.setObjectName("sectionTitle")
        manual_layout.addWidget(manual_title)
        self.cpu_slider, self.cpu_spin = self._fan_row(manual_layout, "CPU fan")
        self.gpu_slider, self.gpu_spin = self._fan_row(manual_layout, "GPU fan")
        self.cpu_slider.valueChanged.connect(
            lambda value: self._mirror_manual_value(self.cpu_slider, value)
        )
        self.gpu_slider.valueChanged.connect(
            lambda value: self._mirror_manual_value(self.gpu_slider, value)
        )

        self.mirror_fans_checkbox = QCheckBox("Mirror fan speeds")
        self.mirror_fans_checkbox.setToolTip(
            "Keep the CPU and GPU manual fan targets at the same percentage"
        )
        self.mirror_fans_checkbox.toggled.connect(self._mirror_manual_toggled)
        manual_layout.addWidget(self.mirror_fans_checkbox)

        warning = QLabel(
            "Changes apply automatically. Values below 30% may stop a fan and "
            "require confirmation."
        )
        warning.setObjectName("warningText")
        warning.setWordWrap(True)
        manual_layout.addWidget(warning)
        manual_layout.addStretch()
        columns.addWidget(self.manual_panel, 1)

        self.sensor_panel = QFrame()
        self.sensor_panel.setObjectName("panel")
        sensor_layout = QVBoxLayout(self.sensor_panel)
        sensor_layout.setContentsMargins(18, 14, 18, 14)
        sensor_layout.setSpacing(10)
        sensor_title = QLabel("Sensor values")
        sensor_title.setObjectName("sectionTitle")
        sensor_layout.addWidget(sensor_title)

        temperature_row = QHBoxLayout()
        temperature_row.setSpacing(12)
        self.cpu_temp_gauge = SensorGauge("CPU temp", "C", temperature_colors=True)
        self.gpu_temp_gauge = SensorGauge("GPU temp", "C", temperature_colors=True)
        temperature_row.addWidget(self.cpu_temp_gauge, 1)
        temperature_row.addWidget(self.gpu_temp_gauge, 1)
        sensor_layout.addLayout(temperature_row)

        fan_row = QHBoxLayout()
        fan_row.setSpacing(12)
        self.cpu_fan_gauge = SensorGauge(
            "CPU fan", "%", temperature_colors=False
        )
        self.gpu_fan_gauge = SensorGauge(
            "GPU fan", "%", temperature_colors=False
        )
        fan_row.addWidget(self.cpu_fan_gauge, 1)
        fan_row.addWidget(self.gpu_fan_gauge, 1)
        sensor_layout.addLayout(fan_row)
        power_row = QHBoxLayout()
        power_row.setSpacing(12)
        self.cpu_power_gauge = SensorGauge(
            "CPU power", "W", temperature_colors=False, maximum=CPU_POWER_GAUGE_MAX_WATTS, decimals=1
        )
        self.gpu_power_gauge = SensorGauge(
            "GPU power", "W", temperature_colors=False, maximum=GPU_POWER_GAUGE_MAX_WATTS, decimals=1
        )
        power_row.addWidget(self.cpu_power_gauge, 1)
        power_row.addWidget(self.gpu_power_gauge, 1)
        sensor_layout.addLayout(power_row)
        self.fault_label = QLabel()
        self.fault_label.setObjectName("warningText")
        self.fault_label.setWordWrap(True)
        self.fault_label.hide()
        sensor_layout.addWidget(self.fault_label)
        sensor_layout.addStretch()
        self.boost_button = QPushButton("Fan Boost 100%")
        self.boost_button.setCheckable(True)
        self.boost_button.setToolTip("Toggle the EC 100% fan override")
        self.boost_button.toggled.connect(self.toggle_boost)
        sensor_layout.addWidget(self.boost_button)
        self.oem_service_button = QPushButton("Stop GCUBridge")
        self.oem_service_button.setToolTip(
            "Start or stop the OEM fan-control service after confirmation"
        )
        self.oem_service_button.clicked.connect(self.toggle_oem_service)
        self.oem_service_button.setVisible(not self._linux)
        sensor_layout.addWidget(self.oem_service_button)
        columns.addWidget(self.sensor_panel, 1)

        self._build_gpu_power_row(layout)
        self._update_mode_panels()

    def _build_gpu_power_row(self, parent_layout: QVBoxLayout) -> None:
        self.gpu_power_panel = ControlPanel()
        self.gpu_power_panel.setObjectName("panel")
        column = QVBoxLayout(self.gpu_power_panel)
        column.setContentsMargins(18, 10, 18, 10)
        column.setSpacing(4)
        controls = QHBoxLayout()
        controls.setSpacing(16)
        title = QLabel("GPU power limit")
        title.setObjectName("fanName")
        controls.addWidget(title)
        self.gpu_power_slider = QSlider(Qt.Orientation.Horizontal)
        self.gpu_power_slider.setRange(0, DEFAULT_GPU_POWER_MAX_OFFSET)
        self.gpu_power_slider.setSingleStep(GPU_POWER_STEP_WATTS)
        self.gpu_power_slider.setPageStep(GPU_POWER_STEP_WATTS)
        self.gpu_power_slider.setTickInterval(GPU_POWER_STEP_WATTS)
        self.gpu_power_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.gpu_power_slider.setAccessibleName("GPU sustained power limit")
        self.gpu_power_slider.valueChanged.connect(self._gpu_power_value_changed)
        self.gpu_power_slider.sliderReleased.connect(self._schedule_gpu_power_apply)
        self.gpu_power_slider.actionTriggered.connect(
            lambda _action: None
            if self.gpu_power_slider.isSliderDown()
            else self._schedule_gpu_power_apply()
        )
        controls.addWidget(self.gpu_power_slider, 1)
        self.dynamic_boost_checkbox = QCheckBox("Dynamic Boost")
        self.dynamic_boost_checkbox.setToolTip(
            "Let the GPU borrow up to 25 W more when the CPU is lightly loaded, "
            "never above the GPU's maximum limit. Needs nvidia-powerd."
        )
        self.dynamic_boost_checkbox.toggled.connect(self._dynamic_boost_toggled)
        controls.addWidget(self.dynamic_boost_checkbox)
        column.addLayout(controls)

        result = QHBoxLayout()
        result.setSpacing(10)
        self.gpu_power_equation_label = QLabel(
            "Reading GPU power limit..."
            if self._linux
            else "GPU power control is available on Linux only for now."
        )
        self.gpu_power_equation_label.setObjectName("statusText")
        result.addWidget(self.gpu_power_equation_label)
        self.gpu_power_limit_badge = QLabel("-- W")
        self.gpu_power_limit_badge.setObjectName("limitBadgePending")
        self.gpu_power_limit_badge.setToolTip("Power limit the NVIDIA driver enforces now")
        self.gpu_power_limit_badge.setAccessibleName("NVIDIA power limit")
        result.addWidget(self.gpu_power_limit_badge)
        self.gpu_power_max_label = QLabel()
        self.gpu_power_max_label.setObjectName("statusText")
        result.addWidget(self.gpu_power_max_label)
        result.addStretch()
        column.addLayout(result)
        self.gpu_power_panel.setEnabled(False)
        parent_layout.addWidget(self.gpu_power_panel)

    def _fan_row(
        self, parent_layout: QVBoxLayout, title: str
    ) -> tuple[QSlider, QSpinBox]:
        labels = QHBoxLayout()
        name = QLabel(title)
        name.setObjectName("fanName")
        labels.addWidget(name)
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
        slider.sliderReleased.connect(lambda: self._manual_input_finished(slider))
        slider.actionTriggered.connect(
            lambda _action: self._manual_slider_action(slider)
        )
        spin.editingFinished.connect(lambda: self._manual_input_finished(spin))
        return slider, spin

    def _temperature_row(
        self, parent_layout: QVBoxLayout, title: str, value: int
    ) -> tuple[QSlider, QSpinBox]:
        label = QLabel(title)
        label.setObjectName("fanName")
        parent_layout.addWidget(label)

        controls = QHBoxLayout()
        controls.setSpacing(14)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        slider.setSingleStep(1)
        slider.setPageStep(5)
        slider.setTickInterval(10)
        slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        slider.setValue(value)
        controls.addWidget(slider, 1)

        spin = QSpinBox()
        spin.setRange(0, 100)
        spin.setSuffix(" C")
        spin.setFixedWidth(88)
        spin.setValue(value)
        controls.addWidget(spin)
        parent_layout.addLayout(controls)

        slider.valueChanged.connect(spin.setValue)
        spin.valueChanged.connect(slider.setValue)
        return slider, spin

    def _apply_style(self) -> None:
        if hasattr(sys, "_MEIPASS"):
            stylesheet_path = Path(sys._MEIPASS) / "frontend" / STYLESHEET_NAME
        else:
            stylesheet_path = Path(__file__).resolve().with_name(STYLESHEET_NAME)
        stylesheet = stylesheet_path.read_text(encoding="utf-8")
        self.setStyleSheet(stylesheet.replace(
            "@CHECKMARK_PATH@", (stylesheet_path.parent / "checkmark.svg").as_posix()
        ))

    @staticmethod
    def _icon_path() -> Path:
        if hasattr(sys, "_MEIPASS"):
            return Path(sys._MEIPASS) / "assets" / ICON_FILENAME
        return Path(__file__).resolve().parents[1] / "assets" / ICON_FILENAME

    def _setup_tray(self) -> None:
        icon = QIcon(str(self._icon_path()))
        self.setWindowIcon(icon)
        self.tray_icon = QSystemTrayIcon(icon, self)
        self.tray_icon.setToolTip("Fan Control")
        tray_menu = QMenu(self)
        show_action = QAction("Show Fan Control", self)
        show_action.triggered.connect(self.activate_from_second_instance)
        tray_menu.addAction(show_action)
        about_action = QAction("About", self)
        about_action.setToolTip("Open the project website on GitHub")
        about_action.triggered.connect(self.open_project_website)
        tray_menu.addAction(about_action)
        tray_menu.addSeparator()
        self.tray_exit_action = QAction("Quit" if self._linux else "Exit", self)
        self.tray_exit_action.triggered.connect(self.request_exit)
        tray_menu.addAction(self.tray_exit_action)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._tray_activated)
        self.tray_icon.show()

    @staticmethod
    def _settings_path() -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().with_suffix(".json")
        if is_linux():
            # The Linux install directory is root-owned; keep GUI preferences per user.
            config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
            return Path(config_home) / LINUX_CONFIG_DIRECTORY / SETTINGS_FILENAME
        return Path(__file__).resolve().parents[1] / SETTINGS_FILENAME

    @classmethod
    def _load_preferences(cls) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "minimum_temp": DEFAULT_MIN_FAN_TEMP,
            "maximum_temp": DEFAULT_MAX_FAN_TEMP,
            "manual_cpu": 50,
            "manual_gpu": 50,
            "mirror_fans": False,
            "start_minimized": True,
        }
        try:
            loaded = json.loads(cls._settings_path().read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                return defaults
            minimum = int(loaded.get("minimum_temp", defaults["minimum_temp"]))
            maximum = int(loaded.get("maximum_temp", defaults["maximum_temp"]))
            cpu = int(loaded.get("manual_cpu", defaults["manual_cpu"]))
            gpu = int(loaded.get("manual_gpu", defaults["manual_gpu"]))
            mirror = loaded.get("mirror_fans", defaults["mirror_fans"])
            if not 0 <= minimum <= maximum <= 100:
                return defaults
            if not 0 <= cpu <= 100 or not 0 <= gpu <= 100:
                return defaults
            if not isinstance(mirror, bool):
                return defaults
            return {
                "minimum_temp": minimum,
                "maximum_temp": maximum,
                "manual_cpu": cpu,
                "manual_gpu": gpu,
                "mirror_fans": mirror,
                "start_minimized": (
                    loaded["start_minimized"]
                    if isinstance(loaded.get("start_minimized"), bool)
                    else defaults["start_minimized"]
                ),
            }
        except (OSError, ValueError, TypeError):
            return defaults

    def _apply_preferences_to_controls(self) -> None:
        self._syncing = True
        try:
            self.mode_toggle.blockSignals(True)
            self.mode_toggle.setChecked(False)
            self.mode_toggle.blockSignals(False)
            self._set_auto_temperatures(
                int(self._preferences["minimum_temp"]),
                int(self._preferences["maximum_temp"]),
            )
            self.cpu_slider.setValue(int(self._preferences["manual_cpu"]))
            self.gpu_slider.setValue(int(self._preferences["manual_gpu"]))
            self.mirror_fans_checkbox.setChecked(
                bool(self._preferences["mirror_fans"])
            )
            self.start_minimized_checkbox.setChecked(
                bool(self._preferences["start_minimized"])
            )
        finally:
            self._syncing = False
        self._last_manual_values = (self.cpu_slider.value(), self.gpu_slider.value())
        self._update_mode_panels()

    def _save_preferences(self) -> None:
        settings = {
            "minimum_temp": self.min_temp_spin.value(),
            "maximum_temp": self.max_temp_spin.value(),
            "manual_cpu": self.cpu_spin.value(),
            "manual_gpu": self.gpu_spin.value(),
            "mirror_fans": self.mirror_fans_checkbox.isChecked(),
            "start_minimized": self.start_minimized_checkbox.isChecked(),
        }
        path = self._settings_path()
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(settings, indent=2) + "\n", encoding="utf-8"
            )
            temporary.replace(path)
        except OSError as exc:
            self._set_status(f"Could not save settings | {exc}")

    def _start_minimized_toggled(self, enabled: bool) -> None:
        if not self._syncing:
            self._preferences["start_minimized"] = enabled
            self._save_preferences()

    def show_on_startup(self) -> None:
        if not self.start_minimized_checkbox.isChecked():
            self.show()
        elif not QSystemTrayIcon.isSystemTrayAvailable():
            self.showMinimized()

    def open_project_website(self) -> None:
        if not QDesktopServices.openUrl(QUrl(PROJECT_URL)):
            self._set_status(f"Could not open a browser | {PROJECT_URL}")

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.activate_from_second_instance()

    def _manual_slider_action(self, slider: QSlider) -> None:
        if not slider.isSliderDown():
            self._schedule_manual_apply()

    def _mirror_manual_value(self, source: QSlider, value: int) -> None:
        if self._syncing or not self.mirror_fans_checkbox.isChecked():
            return
        target = self.gpu_slider if source is self.cpu_slider else self.cpu_slider
        self._syncing = True
        try:
            target.setValue(value)
        finally:
            self._syncing = False

    def _mirror_manual_toggled(self, enabled: bool) -> None:
        if self._syncing:
            return
        if enabled:
            self._syncing = True
            try:
                self.gpu_slider.setValue(self.cpu_slider.value())
            finally:
                self._syncing = False
        self._save_preferences()
        if enabled:
            self._schedule_manual_apply()

    def _manual_input_finished(self, control: QSlider | QSpinBox) -> None:
        rounded = ((control.value() + 2) // 5) * 5
        control.setValue(min(100, rounded))
        self._schedule_manual_apply()

    def _schedule_manual_apply(self) -> None:
        if self._syncing or self._busy or not self.mode_toggle.is_manual():
            return
        self._manual_apply_timer.start()

    def _set_busy(self, busy: bool, message: str | None = None) -> None:
        self._busy = busy
        self.boost_button.setEnabled(not busy)
        self.oem_service_button.setEnabled(not busy)
        self.exit_button.setEnabled(not busy or self._linux)
        self.tray_exit_action.setEnabled(not busy or self._linux)
        self.mode_toggle.setEnabled(not busy)
        self._update_mode_panels()
        self._update_gpu_power_enabled()
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

    def _mode_changed(self, manual: bool) -> None:
        self._update_mode_panels()
        minimum_temp = self.min_temp_spin.value()
        maximum_temp = self.max_temp_spin.value()

        def complete(result: dict[str, Any]) -> None:
            self._show_backend_state(result)
            self._save_preferences()
            if manual:
                self._set_status("Manual mode | Speed changes apply automatically")
            else:
                self._set_status("Automatic mode | Backend control started")
            QTimer.singleShot(0, self.refresh_telemetry)

        self._run(
            lambda: self._backend.request(
                "set_mode",
                automatic=not manual,
                minimum_temp=minimum_temp,
                maximum_temp=maximum_temp,
            ),
            complete,
            "Switching control mode...",
        )

    def _update_mode_panels(self) -> None:
        automatic = not self.mode_toggle.is_manual()
        self.auto_panel.setEnabled(automatic and not self._busy)
        self.manual_panel.setEnabled(not automatic and not self._busy)

    def _set_auto_temperatures(self, minimum_temp: int, maximum_temp: int) -> None:
        minimum_temp = max(0, min(100, minimum_temp))
        maximum_temp = max(minimum_temp, min(100, maximum_temp))
        self._curve_syncing = True
        try:
            self.min_temp_slider.setValue(minimum_temp)
            self.min_temp_spin.setValue(minimum_temp)
            self.max_temp_slider.setValue(maximum_temp)
            self.max_temp_spin.setValue(maximum_temp)
        finally:
            self._curve_syncing = False
        self.curve_graph.set_temperatures(minimum_temp, maximum_temp)

    def _minimum_temp_changed(self, value: int) -> None:
        if self._curve_syncing:
            return
        maximum_temp = self.max_temp_spin.value()
        if value > maximum_temp:
            maximum_temp = value
        self._set_auto_temperatures(value, maximum_temp)

    def _maximum_temp_changed(self, value: int) -> None:
        if self._curve_syncing:
            return
        minimum_temp = self.min_temp_spin.value()
        if value < minimum_temp:
            minimum_temp = value
        self._set_auto_temperatures(minimum_temp, value)

    def _reset_auto_temperatures(self) -> None:
        self._set_auto_temperatures(DEFAULT_MIN_FAN_TEMP, DEFAULT_MAX_FAN_TEMP)
        self._configure_auto()

    def _configure_auto(self) -> None:
        if self.mode_toggle.is_manual() or self._curve_syncing:
            return
        minimum_temp = self.min_temp_spin.value()
        maximum_temp = self.max_temp_spin.value()

        def complete(result: dict[str, Any]) -> None:
            self._show_backend_state(result)
            self._save_preferences()

        self._run(
            lambda: self._backend.request(
                "configure_auto",
                minimum_temp=minimum_temp,
                maximum_temp=maximum_temp,
            ),
            complete,
            "Updating automatic curve...",
        )

    def _run(
        self,
        operation: Callable[[], Any],
        on_complete: Callable[[Any], None],
        busy_message: str,
        on_failed: Callable[[str], None] | None = None,
        *,
        show_error: bool = True,
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
            try:
                on_complete(result)
            except Exception:
                # A malformed reply must not take the GUI down.
                self._set_status("Unexpected backend reply | See the error details")
                QMessageBox.warning(self, "Fan control", traceback.format_exc())

        def failed(details: str) -> None:
            self._workers.discard(worker)
            if self._closing:
                return
            self._set_busy(False, "Fan-control backend communication failed")
            if on_failed is not None:
                on_failed(details)
            if show_error:
                QMessageBox.critical(self, "Fan control error", details)

        worker.signals.completed.connect(complete)
        worker.signals.failed.connect(failed)
        self._pool.start(worker)

    def load_state(self) -> None:
        self._run(
            lambda: self._backend.request("load_state"),
            self._show_state,
            "Reading fan state...",
        )

    def _load_initial_state(self) -> None:
        if self._linux:
            # The always-running service owns the mode; a GUI login must not
            # reset it. The service itself starts in Automatic after every boot.
            self._load_state_quietly()
            return

        def complete(result: dict[str, Any]) -> None:
            self._show_state(result)
            self._apply_preferences_to_controls()
            self._mode_changed(False)

        self._run(
            lambda: self._backend.request("load_state"),
            complete,
            "Reading fan state...",
        )

    def _load_state_quietly(self) -> None:
        self._last_state_attempt = time.monotonic()
        self._run(
            lambda: self._backend.request("load_state"),
            self._show_state,
            "Reading fan state...",
            on_failed=self._show_service_problem,
            show_error=False,
        )

    def _show_service_problem(self, details: str) -> None:
        lines = details.strip().splitlines()
        message = lines[-1] if lines else "unknown error"
        if "BackendUnavailable" in message:
            self._backend_offline = True
            self._set_status(f"Fan-control service unavailable | {SERVICE_START_HINT}")
            return
        self._set_status(f"Fan-control service error | {message.split(': ', 1)[-1]}")

    def _show_state(self, result: dict[str, Any]) -> None:
        self._state_loaded = True
        status = result["status"]
        curve = result["curve"]
        backend = result["backend"]
        self._table_name = str(status["FAN_TableName"])
        self._control_method = backend.get("control_method")
        self._syncing = True
        try:
            if not bool(backend["automatic"]):
                self.cpu_slider.setValue(int(curve["CPU"][0]["Duty"]))
                self.gpu_slider.setValue(int(curve["GPU"][0]["Duty"]))
            self.boost_button.blockSignals(True)
            self.boost_button.setChecked(str(status["FanBoostEnable"]) == "1")
            self.boost_button.blockSignals(False)
            self.mode_toggle.blockSignals(True)
            self.mode_toggle.setChecked(not bool(backend["automatic"]))
            self.mode_toggle.blockSignals(False)
            self._set_auto_temperatures(
                int(backend["minimum_temp"]), int(backend["maximum_temp"])
            )
        finally:
            self._syncing = False
        if not bool(backend["automatic"]):
            self._last_manual_values = (
                self.cpu_slider.value(),
                self.gpu_slider.value(),
            )
        self._update_mode_panels()
        self._show_telemetry(result["telemetry"])
        self._show_backend_state(backend)
        if result["temperatures"] is not None:
            self._show_temperatures(result["temperatures"])
        elif result["temperature_error"]:
            self._show_temperature_error(result["temperature_error"])
        QTimer.singleShot(0, self.refresh_telemetry)

    def refresh_telemetry(self) -> None:
        if self._busy or self._telemetry_inflight or self._closing:
            return
        self._telemetry_inflight = True
        worker = Worker(lambda: self._backend.request("read_telemetry"))
        self._workers.add(worker)

        def complete(result: dict[str, Any]) -> None:
            self._workers.discard(worker)
            self._telemetry_inflight = False
            if self._closing:
                return
            try:
                self._show_telemetry(result["telemetry"])
                self._show_backend_state(result["backend"])
                temperatures = result["temperatures"]
                if temperatures is not None:
                    self._show_temperatures(temperatures)
                elif result["temperature_error"]:
                    self._show_temperature_error(result["temperature_error"])
                if "gpu_power" in result:
                    self._show_gpu_power(result["gpu_power"])
            except (KeyError, TypeError, ValueError) as exc:
                self._set_status(f"Connected | Unexpected telemetry: {exc}")

        def failed(details: str) -> None:
            self._workers.discard(worker)
            self._telemetry_inflight = False
            if not self._closing:
                self._set_status("Connected | Telemetry temporarily unavailable")

        worker.signals.completed.connect(complete)
        worker.signals.failed.connect(failed)
        self._pool.start(worker)

    def _show_telemetry(self, telemetry: dict[str, Any]) -> None:
        for gauge, key, rpm_key in (
            (self.cpu_fan_gauge, "CpuFanDuty", "CpuFanRpm"),
            (self.gpu_fan_gauge, "GpuFanDuty", "GpuFanRpm"),
        ):
            try:
                duty = float(telemetry[key])
            except (KeyError, TypeError, ValueError):
                duty = None
            if duty is not None and not 0 <= duty <= 100:
                duty = None
            rpm = telemetry.get(rpm_key)
            detail = f"{int(rpm)} RPM" if isinstance(rpm, (int, float)) else ""
            gauge.set_value(duty, detail=detail)
        self._set_fault(
            "fan",
            "The EC reports a fan fault. Check both fans."
            if telemetry.get("FanAbnormal")
            else None,
        )
        if self._table_name and self.mode_toggle.is_manual():
            method = {
                "oem_mqtt": "OEM MQTT",
                "direct_ec": "Direct EC",
            }.get(self._control_method, "Fan control")
            self._set_status(
                f"Connected | {method} | Active table: {self._table_name}",
                updated=True,
            )

    def _show_temperatures(self, temperatures: dict[str, Any]) -> None:
        self.cpu_temp_gauge.set_value(float(temperatures["cpu_c"]))
        gpu_c = temperatures.get("gpu_c")
        if gpu_c is None and temperatures.get("gpu_powered_off"):
            self.gpu_temp_gauge.set_value(None, placeholder="Off")
        else:
            self.gpu_temp_gauge.set_value(None if gpu_c is None else float(gpu_c))
        self.cpu_temp_gauge.setToolTip(f"Source: {temperatures['cpu_source']}")
        self.gpu_temp_gauge.setToolTip(f"Source: {temperatures['gpu_source']}")
        self._update_power_draw(temperatures)
        self._set_fault("sensor", None)

    def _show_temperature_error(self, error: str) -> None:
        for gauge in (
            self.cpu_temp_gauge,
            self.gpu_temp_gauge,
            self.cpu_power_gauge,
            self.gpu_power_gauge,
        ):
            gauge.set_value(None)
        self._set_fault("sensor", f"Sensor error: {error}")

    def _set_fault(self, kind: str, message: str | None) -> None:
        # One warning line; the most important message wins.
        self._faults[kind] = message
        for name in ("emergency", "sensor", "fan"):
            if self._faults.get(name):
                self.fault_label.setText(self._faults[name])
                self.fault_label.show()
                return
        self.fault_label.hide()

    def _show_backend_state(self, backend: dict[str, Any]) -> None:
        self._control_method = backend.get("control_method")
        if self._control_method == "oem_mqtt":
            self.oem_service_button.setText("Stop GCUBridge")
            self.control_method_label.setText("OEM MQTT")
        elif self._control_method == "direct_ec":
            self.oem_service_button.setText("Start GCUBridge")
            self.control_method_label.setText("Direct EC")
        else:
            self.control_method_label.setText("Detecting control...")
        capabilities = backend.get("capabilities") or {}
        if not capabilities.get("gpu_power_limit"):
            self._gpu_power_available = False
            self._update_gpu_power_enabled()
        target = backend.get("auto_target")
        self.auto_target_label.setText(
            "Shared target: --%" if target is None else f"Shared target: {target}%"
        )
        if backend.get("sensor_emergency"):
            reason = backend.get("sensor_error") or "no valid reading"
            self._set_fault(
                "emergency",
                f"Sensor failure for over 30 s ({reason}). Both fans are forced to "
                "100% until the sensors recover.",
            )
            self._set_status("Sensor failure | Fans forced to 100%")
            return
        self._set_fault("emergency", None)
        if not backend.get("automatic"):
            return
        error = backend.get("auto_error")
        hottest = backend.get("auto_hottest")
        if error:
            self._set_status(f"Automatic mode error | {error}")
        elif hottest is not None:
            self._set_status(
                f"Automatic mode | Max temperature {float(hottest):.1f} C",
                updated=True,
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
        if self._syncing or self._busy or not self.mode_toggle.is_manual():
            return
        cpu = self.cpu_spin.value()
        gpu = self.gpu_spin.value()
        if (cpu, gpu) == self._last_manual_values:
            return
        if not self._confirm_low_values(cpu, gpu):
            self._syncing = True
            try:
                self.cpu_slider.setValue(self._last_manual_values[0])
                self.gpu_slider.setValue(self._last_manual_values[1])
            finally:
                self._syncing = False
            return

        def complete(result: dict[str, Any]) -> None:
            self._last_manual_values = (int(result["cpu"]), int(result["gpu"]))
            self._save_preferences()
            self.boost_button.blockSignals(True)
            self.boost_button.setChecked(False)
            self.boost_button.blockSignals(False)
            self._set_status(
                f"Applied CPU {result['cpu']}% | GPU {result['gpu']}% | Ramping..."
            )
            QTimer.singleShot(4000, self.refresh_telemetry)

        self._run(
            lambda: self._backend.request(
                "apply_manual",
                cpu=cpu,
                gpu=gpu,
                confirmed_low=cpu < 30 or gpu < 30,
            ),
            complete,
            "Applying manual fan speeds...",
        )

    @staticmethod
    def _auto_target(
        temperature: float,
        minimum_temp: int = DEFAULT_MIN_FAN_TEMP,
        maximum_temp: int = DEFAULT_MAX_FAN_TEMP,
    ) -> int:
        return auto_target(temperature, minimum_temp, maximum_temp)

    def toggle_boost(self, enabled: bool) -> None:
        def complete(state: bool) -> None:
            self._set_status(
                "Fan Boost enabled" if state else "Manual fan control restored"
            )
            QTimer.singleShot(1500, self.refresh_telemetry)

        self._run(
            lambda: self._backend.request("set_boost", enabled=enabled),
            complete,
            "Enabling Fan Boost..." if enabled else "Disabling Fan Boost...",
        )

    def _update_gpu_power_enabled(self) -> None:
        self.gpu_power_panel.setEnabled(self._gpu_power_available and not self._busy)

    def _gpu_power_label_text(self, offset: int) -> str:
        if self._gpu_power_base is None:
            return f"+{offset} W"
        return f"{self._gpu_power_base + offset:.0f} W"

    def _gpu_power_value_changed(self, value: int) -> None:
        del value
        self._update_gpu_power_text()

    def _schedule_gpu_power_apply(self) -> None:
        if not self._gpu_power_available or self._busy:
            return
        step = GPU_POWER_STEP_WATTS
        rounded = min(
            self.gpu_power_slider.maximum(),
            (self.gpu_power_slider.value() + step // 2) // step * step,
        )
        if rounded != self.gpu_power_slider.value():
            self.gpu_power_slider.setValue(rounded)
        self._gpu_power_apply_timer.start()

    def apply_gpu_power(self) -> None:
        if not self._gpu_power_available or self._busy:
            return
        offset = self.gpu_power_slider.value()
        if offset == self._gpu_power_applied:
            return

        def complete(state: dict[str, Any]) -> None:
            self._mark_gpu_power_changed()
            self._show_gpu_power(state)
            self._set_status(
                f"GPU power limit set to {self._gpu_power_label_text(offset)}"
            )

        def failed(_details: str) -> None:
            if self._gpu_power_applied is not None:
                self.gpu_power_slider.blockSignals(True)
                self.gpu_power_slider.setValue(self._gpu_power_applied)
                self.gpu_power_slider.blockSignals(False)
                self._gpu_power_value_changed(self._gpu_power_applied)

        self._run(
            lambda: self._backend.request("set_gpu_power", offset=offset),
            complete,
            "Setting GPU power limit...",
            on_failed=failed,
        )

    @staticmethod
    def _set_label_warning(label: QLabel, warning: bool) -> None:
        normal = label.property("normalObjectName")
        if normal is None:
            normal = label.objectName() if label.objectName() != "warningText" else "statusText"
            label.setProperty("normalObjectName", normal)
        name = "warningText" if warning else normal
        if label.objectName() != name:
            label.setObjectName(name)
            label.style().unpolish(label)
            label.style().polish(label)

    def _dynamic_boost_toggled(self, enabled: bool) -> None:
        if not self._gpu_power_available or self._busy:
            return

        def complete(state: dict[str, Any]) -> None:
            self._mark_gpu_power_changed()
            self._show_gpu_power(state)
            self._set_status("Dynamic Boost enabled" if enabled else "Dynamic Boost disabled")

        def failed(_details: str) -> None:
            self.dynamic_boost_checkbox.blockSignals(True)
            self.dynamic_boost_checkbox.setChecked(not enabled)
            self.dynamic_boost_checkbox.blockSignals(False)

        self._run(
            lambda: self._backend.request("set_dynamic_boost", enabled=enabled),
            complete,
            "Enabling Dynamic Boost..." if enabled else "Disabling Dynamic Boost...",
            on_failed=failed,
        )

    def _mark_gpu_power_changed(self) -> None:
        # Until NVIDIA reports a limit read after this moment, the badge shows "?".
        self._gpu_power_changed_at = time.monotonic()
        for delay in GPU_LIMIT_REFRESH_DELAYS_MS:
            QTimer.singleShot(delay, self.refresh_telemetry)

    def _gpu_limit_is_fresh(self) -> bool:
        changed_at = self._gpu_power_changed_at
        if changed_at is None:
            return True
        age = self._gpu_power_state.get("limits_age_s")
        read_at = None if not isinstance(age, (int, float)) else self._gpu_power_state_at - age
        if (read_at is not None and read_at >= changed_at + GPU_LIMIT_SETTLE_SECONDS) or (
            time.monotonic() - changed_at >= GPU_LIMIT_WAIT_SECONDS
        ):
            self._gpu_power_changed_at = None
            return True
        return False

    def _show_gpu_power(self, state: dict[str, Any]) -> None:
        self._gpu_power_state = dict(state)
        self._gpu_power_state_at = time.monotonic()
        self._gpu_power_available = bool(state.get("available"))
        base = state.get("base_limit_w")
        if isinstance(base, (int, float)):
            self._gpu_power_base = float(base)
        max_offset = state.get("max_offset")
        if isinstance(max_offset, int) and max_offset >= 0:
            self.gpu_power_slider.setMaximum(max_offset)
        maximum = state.get("max_limit_w")
        if isinstance(maximum, (int, float)):
            self.gpu_power_gauge.set_maximum(float(maximum))
        offset = state.get("offset")
        if isinstance(offset, int):
            self._gpu_power_applied = offset
            if (
                not self.gpu_power_slider.isSliderDown()
                and not self._gpu_power_apply_timer.isActive()
            ):
                self.gpu_power_slider.blockSignals(True)
                self.gpu_power_slider.setValue(offset)
                self.gpu_power_slider.blockSignals(False)
        boost = state.get("dynamic_boost")
        if isinstance(boost, bool):
            self.dynamic_boost_checkbox.blockSignals(True)
            self.dynamic_boost_checkbox.setChecked(boost)
            self.dynamic_boost_checkbox.blockSignals(False)
        self._update_gpu_power_text()
        self._update_gpu_power_enabled()

    def _set_limit_badge(self, text: str, kind: str, tooltip: str) -> None:
        name = {"ok": "limitBadge", "warning": "limitBadgeWarning"}.get(kind, "limitBadgePending")
        self.gpu_power_limit_badge.setText(text)
        self.gpu_power_limit_badge.setToolTip(tooltip)
        if self.gpu_power_limit_badge.objectName() != name:
            self.gpu_power_limit_badge.setObjectName(name)
            self.gpu_power_limit_badge.style().unpolish(self.gpu_power_limit_badge)
            self.gpu_power_limit_badge.style().polish(self.gpu_power_limit_badge)

    def _update_gpu_power_text(self) -> None:
        """Show sustained + boost = NVIDIA limit / maximum, like TUXEDO's TGP chart."""
        state = self._gpu_power_state
        if not state:
            return
        offset = self.gpu_power_slider.value()
        base = self._gpu_power_base
        maximum = state.get("max_limit_w")
        enforced = state.get("enforced_limit_w")
        self.gpu_power_max_label.setText(
            f"/ {maximum:.0f} W max" if isinstance(maximum, (int, float)) else ""
        )
        warning = False
        if not state.get("available"):
            equation = f"Unavailable: {state.get('error') or 'unknown error'}"
            self._set_limit_badge("-- W", "warning", "The GPU power limit cannot be read")
            warning = True
        elif base is None:
            equation = f"Base + {offset} W sustained (base shown once the GPU wakes) ="
            self._set_limit_badge("-- W", "pending", "NVIDIA reports limits while the GPU is awake")
        else:
            sustained = base + offset
            boost_w = state.get("dynamic_boost_w")
            room = float(boost_w) if state.get("dynamic_boost") is True and isinstance(boost_w, int) else 0.0
            if isinstance(maximum, (int, float)):
                room = max(0.0, min(room, maximum - sustained))
            applied = (
                offset == state.get("offset")
                and not self._gpu_power_apply_timer.isActive()
                and not self.gpu_power_slider.isSliderDown()
            )
            if not state.get("powerd_running", True):
                equation = "Not applied: nvidia-powerd is not running. NVIDIA enforces"
                self._set_limit_badge(
                    f"{enforced:.0f} W" if isinstance(enforced, (int, float)) else "-- W",
                    "warning",
                    "NVIDIA applies cTGP and Dynamic Boost only while nvidia-powerd runs",
                )
                warning = True
            elif applied and isinstance(enforced, (int, float)) and not self._gpu_limit_is_fresh():
                equation = f"{sustained:.0f} W sustained + {room:.0f} W boost ="
                self._set_limit_badge(
                    "? W", "pending", "Waiting for NVIDIA to report the new limit"
                )
            elif applied and isinstance(enforced, (int, float)):
                if enforced + 1 < sustained:
                    equation = f"{sustained:.0f} W requested, but NVIDIA enforces"
                    self._set_limit_badge(
                        f"{enforced:.0f} W", "warning", "NVIDIA enforces less than requested"
                    )
                    warning = True
                else:
                    # The boost term is what NVIDIA actually adds right now.
                    equation = (
                        f"{sustained:.0f} W sustained + {enforced - sustained:.0f} W boost ="
                    )
                    self._set_limit_badge(
                        f"{enforced:.0f} W", "ok", "Power limit the NVIDIA driver enforces now"
                    )
            else:
                equation = f"{sustained:.0f} W sustained + {room:.0f} W boost ="
                self._set_limit_badge(
                    f"~{sustained + room:.0f} W",
                    "pending",
                    "Expected limit; NVIDIA reports it while the GPU is awake"
                    if applied
                    else "Expected limit once the change is applied",
                )
        self.gpu_power_equation_label.setText(equation)
        self._set_label_warning(self.gpu_power_equation_label, warning)

    def _update_power_draw(self, temperatures: dict[str, Any]) -> None:
        cpu = temperatures.get("cpu_power_w")
        gpu = temperatures.get("gpu_power_w")
        self.cpu_power_gauge.set_value(float(cpu) if isinstance(cpu, (int, float)) else None)
        if temperatures.get("gpu_powered_off"):
            self.gpu_power_gauge.set_value(None, placeholder="Off")
        else:
            self.gpu_power_gauge.set_value(float(gpu) if isinstance(gpu, (int, float)) else None)

    def toggle_oem_service(self) -> None:
        start_service = self._control_method != "oem_mqtt"
        action = "start" if start_service else "stop"
        if start_service:
            details = (
                "Start the GCUBridge service and switch fan writes back to OEM MQTT?"
            )
        else:
            details = (
                "Stop the GCUBridge service and switch fan writes to direct EC control?\n\n"
                "The OEM Control Center will not control the fans while its service is stopped."
            )
        answer = QMessageBox.question(
            self,
            f"Confirm GCUBridge {action}",
            details,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def complete(result: dict[str, Any]) -> None:
            self._show_state(result)
            method = "OEM MQTT" if start_service else "Direct EC"
            self._set_status(f"GCUBridge {action}ped | {method} active", updated=True)

        self._run(
            lambda: self._backend.request(
                "set_oem_service",
                request_timeout=45.0,
                enabled=start_service,
                confirmed=True,
            ),
            complete,
            f"{action.title()}ping GCUBridge...",
        )

    def check_backend(self) -> None:
        if (
            self._closing
            or self._busy
            or self._telemetry_inflight
            or self._backend_check_inflight
        ):
            return
        self._backend_check_inflight = True
        worker = Worker(lambda: self._backend.request("frontend_heartbeat"))
        self._workers.add(worker)

        def complete(result: dict[str, Any]) -> None:
            self._workers.discard(worker)
            self._backend_check_inflight = False
            if self._closing:
                return
            recovered = self._backend_offline
            self._backend_offline = False
            self._show_backend_state(result)
            if self._linux:
                # The service is authoritative on Linux: adopt its state after an
                # outage, and keep retrying quietly if the first load failed.
                if recovered or (
                    not self._state_loaded
                    and time.monotonic() - self._last_state_attempt >= 30.0
                ):
                    self._load_state_quietly()
            elif recovered:
                self._sync_backend_mode()

        def failed(details: str) -> None:
            del details
            self._workers.discard(worker)
            self._backend_check_inflight = False
            if self._closing:
                return
            self._backend_offline = True
            if self._linux:
                self._set_status(f"Fan-control service unavailable | {SERVICE_START_HINT}")
                return
            self._set_status("Backend unavailable | Waiting to restart")
            now = time.monotonic()
            if now - self._last_backend_restart >= RESTART_COOLDOWN_SECONDS:
                self._last_backend_restart = now
                try:
                    launch_component("backend", "--no-frontend")
                    self._set_status("Backend unavailable | Restart requested")
                except Exception as exc:
                    self._set_status(f"Backend restart failed | {exc}")

        worker.signals.completed.connect(complete)
        worker.signals.failed.connect(failed)
        self._pool.start(worker)

    def _sync_backend_mode(self) -> None:
        automatic = not self.mode_toggle.is_manual()
        minimum_temp = self.min_temp_spin.value()
        maximum_temp = self.max_temp_spin.value()
        self._run(
            lambda: self._backend.request(
                "set_mode",
                automatic=automatic,
                minimum_temp=minimum_temp,
                maximum_temp=maximum_temp,
            ),
            self._show_backend_state,
            "Restoring backend control mode...",
        )

    def request_exit(self) -> None:
        if self._linux:
            # Closing the Linux GUI never changes fan control; the service keeps running.
            answer = QMessageBox.question(
                self,
                "Confirm quit",
                "Close the Fan Control window and tray icon?\n\n"
                "The fan-control service keeps controlling both fans in the background.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._manual_apply_timer.stop()
            self._gpu_power_apply_timer.stop()
            self._exit_prepared = True
            self.close()
            return
        if self._exit_in_progress:
            return
        if self._busy:
            QMessageBox.information(
                self,
                "Fan operation in progress",
                "Wait for the current fan operation to finish before exiting.",
            )
            return
        answer = QMessageBox.question(
            self,
            "Confirm exit",
            "Set both fans to 100% and exit Fan Control?\n\n"
            "The application will remain open if the 100% fan write fails.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self._exit_in_progress = True
        self._manual_apply_timer.stop()

        def complete(_result: dict[str, Any]) -> None:
            self._exit_in_progress = False
            self._exit_prepared = True
            self.close()

        def failed(_details: str) -> None:
            self._exit_in_progress = False

        self._run(
            lambda: self._backend.request(
                "prepare_exit", confirmed=True, request_timeout=45.0
            ),
            complete,
            "Setting both fans to 100% before exit...",
            on_failed=failed,
        )

    def prepare_session_shutdown(self, manager: Any) -> None:
        if self._exit_prepared:
            return
        # Windows must not receive our shutdown acknowledgement before the write.
        # Queue behind any active GUI operation, without running hardware on Qt's
        # thread or processing callbacks that could schedule another fan command.
        self._closing = True
        self._manual_apply_timer.stop()
        outcome: list[str | None] = []

        def prepare() -> None:
            try:
                self._backend.request(
                    "prepare_exit", confirmed=True, request_timeout=40.0
                )
            except Exception as exc:
                outcome.append(str(exc))
            else:
                outcome.append(None)

        worker = Worker(prepare)
        self._pool.start(worker)
        finished = self._pool.waitForDone(45000)
        if not finished or not outcome or outcome[0] is not None:
            manager.cancel()
            self._closing = False
            details = outcome[0] if outcome else "Timed out waiting for fan control"
            self._set_status(f"Shutdown fan write failed | {details}")
            return
        self._exit_prepared = True
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._exit_prepared:
            event.ignore()
            self.hide()
            self.tray_icon.showMessage(
                "Fan Control is still running",
                "The fan-control service keeps running. Use Quit to close the tray icon."
                if self._linux
                else "Use the Exit button or the tray menu to stop fan control.",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )
            return

        self._closing = True
        self._telemetry_timer.stop()
        self._status_timer.stop()
        self._backend_watchdog_timer.stop()
        self._gpu_power_apply_timer.stop()
        try:
            self._backend.request("frontend_detach", request_timeout=0.5)
        except Exception:
            pass
        self.tray_icon.hide()
        event.accept()
        application = QApplication.instance()
        if application is not None:
            QTimer.singleShot(0, application.quit)

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


def main(backend: Any | None = None) -> None:
    linux = is_linux()
    if backend is None and not ensure_backend(start_frontend=False) and not linux:
        raise RuntimeError("Could not start the fan-control backend")
    # On Linux the GUI also starts while the service is down; it shows the
    # outage and reconnects when systemd brings the service back.
    app = QApplication(sys.argv)
    app.setApplicationName("Fan Control")
    app.setDesktopFileName("stellaris-fan-control")
    app.setQuitOnLastWindowClosed(False)
    if notify_existing_instance():
        return
    window = FanControlWindow(backend=backend)
    if not linux:
        # Windows session end writes 100%; on Linux the service handles shutdown.
        app.commitDataRequest.connect(
            window.prepare_session_shutdown, Qt.ConnectionType.DirectConnection
        )
    window._instance_server = create_instance_server(window)
    window.show_on_startup()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
