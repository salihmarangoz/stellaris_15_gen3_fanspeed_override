import json
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRectF, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QCloseEvent, QIcon, QPainter, QPen
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
    RESTART_COOLDOWN_SECONDS,
    ensure_backend,
    launch_component,
)


INSTANCE_SERVER_NAME = "stellaris15gen3.fan-control"
STYLESHEET_NAME = "stellaris15gen3.css"
ICON_FILENAME = "stellaris-fan-control.png"
SETTINGS_FILENAME = "StellarisFanControl.json"


class FanCurveGraph(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._minimum_temp = DEFAULT_MIN_FAN_TEMP
        self._maximum_temp = DEFAULT_MAX_FAN_TEMP
        self.setMinimumHeight(190)

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
    def __init__(self, title: str, unit: str, *, temperature_colors: bool) -> None:
        super().__init__()
        self._title = title
        self._unit = unit
        self._temperature_colors = temperature_colors
        self._value: float | None = None
        self.setMinimumSize(135, 125)
        self.setAccessibleName(f"{title} gauge")

    def set_value(self, value: float | None) -> None:
        self._value = value
        if value is None:
            self.setAccessibleDescription("Value unavailable")
        else:
            self.setAccessibleDescription(f"{value:.1f} {self._unit}")
        self.update()

    def paintEvent(self, event: object) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        title_font = painter.font()
        title_font.setBold(True)
        title_font.setPointSize(11)
        painter.setFont(title_font)
        painter.setPen(QColor("#eef1f3"))
        painter.drawText(
            self.rect().adjusted(0, 0, 0, -94),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            self._title,
        )

        diameter = min(self.width() - 20, 150)
        arc_rect = QRectF((self.width() - diameter) / 2, 32, diameter, diameter)
        background_pen = QPen(QColor("#34393e"), 11)
        background_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(background_pen)
        painter.drawArc(arc_rect, 180 * 16, -180 * 16)

        if self._value is None:
            gauge_color = QColor("#727980")
            span = 0
            value_text = f"-- {self._unit}"
        else:
            displayed = max(0.0, min(100.0, self._value))
            if self._temperature_colors and displayed >= 80:
                gauge_color = QColor("#e35d6a")
            elif self._temperature_colors and displayed >= 60:
                gauge_color = QColor("#e4b45d")
            else:
                gauge_color = QColor("#25b99a")
            span = round(-180 * 16 * displayed / 100)
            decimals = 1 if self._temperature_colors else 0
            value_text = f"{self._value:.{decimals}f} {self._unit}"

        value_pen = QPen(gauge_color, 11)
        value_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(value_pen)
        painter.drawArc(arc_rect, 180 * 16, span)

        value_font = painter.font()
        value_font.setBold(True)
        value_font.setPointSize(14)
        painter.setFont(value_font)
        painter.setPen(gauge_color)
        painter.drawText(
            self.rect().adjusted(0, 66, 0, 0),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            value_text,
        )

        scale_font = painter.font()
        scale_font.setBold(False)
        scale_font.setPointSize(8)
        painter.setFont(scale_font)
        painter.setPen(QColor("#aeb6bd"))
        painter.drawText(5, 105, "0")
        painter.drawText(self.width() - 27, 105, "100")


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
        self.setMinimumSize(1080, 650)
        self.resize(1240, 720)

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
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(18)

        heading_row = QHBoxLayout()
        heading = QLabel("Fan Control")
        heading.setObjectName("heading")
        heading_row.addWidget(heading)
        heading_row.addStretch()
        self.control_method_label = QLabel("Detecting control...")
        self.control_method_label.setObjectName("methodBadge")
        heading_row.addWidget(self.control_method_label)
        self.exit_button = QPushButton("Exit")
        self.exit_button.setObjectName("exitButton")
        self.exit_button.setToolTip("Set both fans to 80% and exit")
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
        layout.addLayout(mode_row)

        columns = QHBoxLayout()
        columns.setSpacing(14)
        layout.addLayout(columns, 1)

        self.auto_panel = ControlPanel()
        self.auto_panel.setObjectName("panel")
        auto_layout = QVBoxLayout(self.auto_panel)
        auto_layout.setContentsMargins(18, 18, 18, 18)
        auto_layout.setSpacing(12)
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
        manual_layout.setContentsMargins(18, 18, 18, 18)
        manual_layout.setSpacing(18)
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
        sensor_layout.setContentsMargins(18, 18, 18, 18)
        sensor_layout.setSpacing(14)
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
        self.source_label = QLabel("CPU: PawnIO Ryzen SMN | GPU: NVIDIA driver")
        self.source_label.setWordWrap(True)
        self.source_label.setObjectName("statusText")
        sensor_layout.addWidget(self.source_label)
        sensor_layout.addStretch()
        self.boost_button = QPushButton("Fan Boost 100%")
        self.boost_button.setCheckable(True)
        self.boost_button.setToolTip("Toggle the OEM 100% fan override")
        self.boost_button.toggled.connect(self.toggle_boost)
        sensor_layout.addWidget(self.boost_button)
        self.oem_service_button = QPushButton("Stop GCUBridge")
        self.oem_service_button.setToolTip(
            "Start or stop the OEM fan-control service after confirmation"
        )
        self.oem_service_button.clicked.connect(self.toggle_oem_service)
        sensor_layout.addWidget(self.oem_service_button)
        columns.addWidget(self.sensor_panel, 1)

        self._update_mode_panels()

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
        self.setStyleSheet(stylesheet_path.read_text(encoding="utf-8"))

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
        tray_menu.addSeparator()
        self.tray_exit_action = QAction("Exit", self)
        self.tray_exit_action.triggered.connect(self.request_exit)
        tray_menu.addAction(self.tray_exit_action)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._tray_activated)
        self.tray_icon.show()

    @staticmethod
    def _settings_path() -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().with_suffix(".json")
        return Path(__file__).resolve().parents[1] / SETTINGS_FILENAME

    @classmethod
    def _load_preferences(cls) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "minimum_temp": DEFAULT_MIN_FAN_TEMP,
            "maximum_temp": DEFAULT_MAX_FAN_TEMP,
            "manual_cpu": 50,
            "manual_gpu": 50,
            "mirror_fans": False,
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
        }
        path = self._settings_path()
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps(settings, indent=2) + "\n", encoding="utf-8"
            )
            temporary.replace(path)
        except OSError as exc:
            self._set_status(f"Could not save settings | {exc}")

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
        self.exit_button.setEnabled(not busy)
        self.tray_exit_action.setEnabled(not busy)
        self.mode_toggle.setEnabled(not busy)
        self._update_mode_panels()
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
            self._set_busy(False, "Fan-control backend communication failed")
            if on_failed is not None:
                on_failed(details)
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
        def complete(result: dict[str, Any]) -> None:
            self._show_state(result)
            self._apply_preferences_to_controls()
            self._mode_changed(False)

        self._run(
            lambda: self._backend.request("load_state"),
            complete,
            "Reading fan state...",
        )

    def _show_state(self, result: dict[str, Any]) -> None:
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
            if not self._closing:
                self._show_telemetry(result["telemetry"])
                self._show_backend_state(result["backend"])
                temperatures = result["temperatures"]
                if temperatures is not None:
                    self._show_temperatures(temperatures)
                elif result["temperature_error"]:
                    self._show_temperature_error(result["temperature_error"])

        def failed(details: str) -> None:
            self._workers.discard(worker)
            self._telemetry_inflight = False
            if not self._closing:
                self._set_status("Connected | Telemetry temporarily unavailable")

        worker.signals.completed.connect(complete)
        worker.signals.failed.connect(failed)
        self._pool.start(worker)

    def _show_telemetry(self, telemetry: dict[str, Any]) -> None:
        for gauge, key in (
            (self.cpu_fan_gauge, "CpuFanDuty"),
            (self.gpu_fan_gauge, "GpuFanDuty"),
        ):
            try:
                duty = float(telemetry[key])
            except (KeyError, TypeError, ValueError):
                duty = None
            if duty is not None and not 0 <= duty <= 100:
                duty = None
            gauge.set_value(duty)
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
        self.gpu_temp_gauge.set_value(float(temperatures["gpu_c"]))
        self.source_label.setText(
            f"CPU: {temperatures['cpu_source']} | GPU: {temperatures['gpu_source']}"
        )

    def _show_temperature_error(self, error: str) -> None:
        self.cpu_temp_gauge.set_value(None)
        self.gpu_temp_gauge.set_value(None)
        self.source_label.setText(f"Sensor error: {error}")

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
        target = backend.get("auto_target")
        self.auto_target_label.setText(
            "Shared target: --%" if target is None else f"Shared target: {target}%"
        )
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
            if recovered:
                self._sync_backend_mode()

        def failed(details: str) -> None:
            del details
            self._workers.discard(worker)
            self._backend_check_inflight = False
            if self._closing:
                return
            self._backend_offline = True
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
            "Set both fans to 80% and exit Fan Control?\n\n"
            "The application will remain open if the 80% fan write fails.",
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
            "Setting both fans to 80% before exit...",
            on_failed=failed,
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._exit_prepared:
            event.ignore()
            self.hide()
            self.tray_icon.showMessage(
                "Fan Control is still running",
                "Use the Exit button or the tray menu to stop fan control.",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )
            return

        self._closing = True
        self._telemetry_timer.stop()
        self._status_timer.stop()
        self._backend_watchdog_timer.stop()
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
    if backend is None and not ensure_backend(start_frontend=False):
        raise RuntimeError("Could not start the fan-control backend")
    app = QApplication(sys.argv)
    app.setApplicationName("Fan Control")
    app.setQuitOnLastWindowClosed(False)
    if notify_existing_instance():
        return
    window = FanControlWindow(backend=backend)
    window._instance_server = create_instance_server(window)
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
