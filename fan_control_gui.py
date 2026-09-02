import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRectF, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QCloseEvent, QPainter, QPen
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from fan_control_service import ControlCenterService
from temperature_service import Temperatures, read_temperatures


INSTANCE_SERVER_NAME = "stellaris15gen3.fan-control"
STYLESHEET_NAME = "stellaris15gen3.css"
AUTO_INTERVAL_MS = 15000
DEFAULT_MIN_FAN_TEMP = 40
DEFAULT_MAX_FAN_TEMP = 80
MIN_AUTO_DUTY = 30
MAX_AUTO_DUTY = 100


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
            point(0, MIN_AUTO_DUTY),
            point(self._minimum_temp, MIN_AUTO_DUTY),
            point(self._maximum_temp, MAX_AUTO_DUTY),
            point(100, MAX_AUTO_DUTY),
        ]
        for start, end in zip(points, points[1:]):
            painter.drawLine(*start, *end)

        painter.setBrush(curve_color)
        for x, y in points[1:3]:
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
        self.setChecked(True)
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
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Fan Control")
        self.setMinimumSize(1080, 650)
        self.resize(1240, 720)

        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._service = ControlCenterService.instance()
        self._busy = False
        self._telemetry_inflight = False
        self._closing = False
        self._syncing = False
        self._curve_syncing = False
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

        heading = QLabel("Fan Control")
        heading.setObjectName("heading")
        layout.addWidget(heading)

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
            "100% between these temperatures."
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

        reset_row = QHBoxLayout()
        reset_row.addStretch()
        self.reset_curve_button = QPushButton("Reset to 40 / 80 C")
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

        warning = QLabel("Values below 30% may stop a fan and require confirmation.")
        warning.setObjectName("warningText")
        warning.setWordWrap(True)
        manual_layout.addWidget(warning)
        manual_layout.addStretch()

        self.apply_button = QPushButton("Apply manual speeds")
        self.apply_button.setObjectName("primaryButton")
        self.apply_button.clicked.connect(self.apply_speeds)
        manual_layout.addWidget(self.apply_button)
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
        slider.valueChanged.connect(self._mark_dirty)
        slider.sliderReleased.connect(
            lambda: slider.setValue(((slider.value() + 2) // 5) * 5)
        )
        spin.editingFinished.connect(
            lambda: spin.setValue(((spin.value() + 2) // 5) * 5)
        )
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
            stylesheet_path = Path(sys._MEIPASS) / STYLESHEET_NAME
        else:
            stylesheet_path = Path(__file__).resolve().with_name(STYLESHEET_NAME)
        self.setStyleSheet(stylesheet_path.read_text(encoding="utf-8"))

    def _mark_dirty(self) -> None:
        if not self._syncing:
            self._dirty = True

    def _set_busy(self, busy: bool, message: str | None = None) -> None:
        self._busy = busy
        self.boost_button.setEnabled(not busy)
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
        if not manual:
            self._auto_backup_needed = True
            self._auto_timer.start()
            QTimer.singleShot(0, self.run_auto_cycle)
        else:
            self._auto_timer.stop()
            self._set_status("Manual mode | Apply speeds when ready")
            QTimer.singleShot(0, self.refresh_telemetry)

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
        QTimer.singleShot(0, self.refresh_telemetry)

    def refresh_telemetry(self) -> None:
        if self._busy or self._telemetry_inflight or self._auto_inflight or self._closing:
            return
        self._telemetry_inflight = True
        read_sensor_values = self.mode_toggle.is_manual()

        def operation() -> dict[str, Any]:
            result: dict[str, Any] = {
                "telemetry": self._service.read_telemetry(),
                "temperatures": None,
                "temperature_error": None,
            }
            if read_sensor_values:
                try:
                    result["temperatures"] = read_temperatures()
                except Exception as exc:
                    result["temperature_error"] = str(exc)
            return result

        worker = Worker(operation)
        self._workers.add(worker)

        def complete(result: dict[str, Any]) -> None:
            self._workers.discard(worker)
            self._telemetry_inflight = False
            if not self._closing:
                self._show_telemetry(result["telemetry"])
                temperatures = result["temperatures"]
                if temperatures is not None:
                    self._show_temperatures(temperatures)
                elif result["temperature_error"]:
                    self.cpu_temp_gauge.set_value(None)
                    self.gpu_temp_gauge.set_value(None)
                    self.source_label.setText(
                        f"Sensor error: {result['temperature_error']}"
                    )

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
            self._set_status(
                f"Connected | Active table: {self._table_name}", updated=True
            )

    def _show_temperatures(self, temperatures: Temperatures) -> None:
        self.cpu_temp_gauge.set_value(temperatures.cpu_c)
        self.gpu_temp_gauge.set_value(temperatures.gpu_c)
        self.source_label.setText(
            f"CPU: {temperatures.cpu_source} | GPU: {temperatures.gpu_source}"
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
    def _auto_target(
        temperature: float,
        minimum_temp: int = DEFAULT_MIN_FAN_TEMP,
        maximum_temp: int = DEFAULT_MAX_FAN_TEMP,
    ) -> int:
        if maximum_temp <= minimum_temp:
            return MAX_AUTO_DUTY if temperature >= maximum_temp else MIN_AUTO_DUTY
        if temperature <= minimum_temp:
            return MIN_AUTO_DUTY
        if temperature >= maximum_temp:
            return MAX_AUTO_DUTY
        position = (temperature - minimum_temp) / (maximum_temp - minimum_temp)
        duty = MIN_AUTO_DUTY + position * (MAX_AUTO_DUTY - MIN_AUTO_DUTY)
        rounded_duty = int((duty + 2.5) // 5) * 5
        return max(MIN_AUTO_DUTY, min(MAX_AUTO_DUTY, rounded_duty))

    def run_auto_cycle(self) -> None:
        if (
            self.mode_toggle.is_manual()
            or self._busy
            or self._telemetry_inflight
            or self._auto_inflight
            or self._closing
        ):
            return

        self._auto_inflight = True
        make_backup = self._auto_backup_needed
        minimum_temp = self.min_temp_spin.value()
        maximum_temp = self.max_temp_spin.value()

        def operation() -> dict[str, Any]:
            temperatures = read_temperatures()
            hottest = max(temperatures.cpu_c, temperatures.gpu_c)
            target = self._auto_target(hottest, minimum_temp, maximum_temp)
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
            self._show_temperatures(temperatures)
            self.auto_target_label.setText(f"Shared target: {applied['cpu']}%")
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
