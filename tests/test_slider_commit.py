import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QAbstractSlider
from frontend.fan_control_gui import FanControlWindow

class SliderCommitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.initial = patch.object(FanControlWindow, '_load_initial_state')
        self.settings = patch.object(FanControlWindow, '_settings_path', return_value=Path(self.temp.name)/'settings.json')
        self.initial.start(); self.settings.start()
        self.addCleanup(self.initial.stop); self.addCleanup(self.settings.stop)
        self.backend = Mock()
        self.window = FanControlWindow(backend=self.backend)
        self.window._telemetry_timer.stop(); self.window._backend_watchdog_timer.stop()
        self.window._run = Mock(side_effect=lambda operation, *args, **kwargs: operation())
        self.window._manual_apply_timer.setInterval(20)
        self.window._auto_configure_timer.setInterval(20)
        self.window._gpu_power_apply_timer.setInterval(20)
        self.addCleanup(self.close_window)

    def close_window(self):
        self.window._closing = True
        self.window._manual_apply_timer.stop(); self.window._auto_configure_timer.stop()
        self.window._gpu_power_apply_timer.stop(); self.window.lightbar_window.stop_pending_apply()
        self.window.tray_icon.hide(); self.window.deleteLater(); self.app.processEvents()

    def manual_mode(self, mirrored=False):
        with patch.object(self.window, '_mode_changed'):
            self.window.mode_toggle.setChecked(True)
        self.window._update_mode_panels()
        if mirrored:
            self.window.mirror_fans_checkbox.setChecked(True)
            self.window._manual_apply_timer.stop()

    def test_each_fan_drag_cancels_pending_work_and_applies_final_value_once(self):
        self.manual_mode()
        for slider, cpu, gpu in ((self.window.cpu_slider, 70, 50), (self.window.gpu_slider, 70, 80)):
            with self.subTest(slider=slider):
                self.backend.reset_mock()
                slider.triggerAction(QAbstractSlider.SliderAction.SliderSingleStepAdd)
                self.assertTrue(self.window._manual_apply_timer.isActive())
                slider.setSliderDown(True)
                self.assertFalse(self.window._manual_apply_timer.isActive())
                for value in (55, 65, cpu if slider is self.window.cpu_slider else gpu):
                    slider.setSliderPosition(value)
                    QTest.qWait(35)
                self.window.apply_speeds()  # a queued timeout must also refuse
                self.backend.request.assert_not_called()
                self.assertFalse(self.window._busy)
                self.assertTrue(slider.isEnabled())
                slider.setSliderDown(False)
                QTest.qWait(40)
                self.backend.request.assert_called_once_with('apply_manual', cpu=cpu, gpu=gpu, confirmed_low=False)

    def test_mirrored_fans_preview_both_but_write_only_after_release(self):
        self.manual_mode(mirrored=True)
        slider = self.window.cpu_slider
        slider.setSliderDown(True); slider.setSliderPosition(67)
        self.assertEqual(self.window.gpu_slider.value(), 67)
        QTest.qWait(40)
        self.backend.request.assert_not_called()
        slider.setSliderDown(False); QTest.qWait(40)
        self.backend.request.assert_called_once_with('apply_manual', cpu=65, gpu=65, confirmed_low=False)

    def test_temperature_sliders_preview_then_configure_once_on_release(self):
        for slider, value in ((self.window.min_temp_slider, 45), (self.window.max_temp_slider, 85)):
            with self.subTest(slider=slider):
                self.backend.reset_mock()
                slider.triggerAction(QAbstractSlider.SliderAction.SliderSingleStepAdd)
                self.assertTrue(self.window._auto_configure_timer.isActive())
                slider.setSliderDown(True)
                self.assertFalse(self.window._auto_configure_timer.isActive())
                slider.setSliderPosition(value)
                QTest.qWait(40); self.window._configure_auto()
                self.backend.request.assert_not_called()
                self.assertTrue(slider.isEnabled())
                slider.setSliderDown(False); QTest.qWait(40)
                self.backend.request.assert_called_once_with('configure_auto', minimum_temp=self.window.min_temp_spin.value(), maximum_temp=self.window.max_temp_spin.value())

    def test_manual_step_actions_and_spin_commit_still_work(self):
        self.manual_mode()
        self.window.cpu_slider.triggerAction(QAbstractSlider.SliderAction.SliderSingleStepAdd)
        QTest.qWait(40)
        self.backend.request.assert_called_once_with('apply_manual', cpu=55, gpu=50, confirmed_low=False)
        self.backend.reset_mock()
        self.window.cpu_spin.setValue(73); self.window.cpu_spin.editingFinished.emit()
        QTest.qWait(40)
        self.backend.request.assert_called_once_with('apply_manual', cpu=75, gpu=50, confirmed_low=False)

    def test_temperature_keyboard_and_spin_commit_still_work(self):
        self.window.min_temp_slider.triggerAction(QAbstractSlider.SliderAction.SliderSingleStepAdd)
        QTest.qWait(40)
        self.assertEqual(self.backend.request.call_args.args, ('configure_auto',))
        self.backend.reset_mock()
        self.window.min_temp_spin.setValue(42); self.window.min_temp_spin.editingFinished.emit()
        QTest.qWait(40)
        self.backend.request.assert_called_once_with('configure_auto', minimum_temp=42, maximum_temp=self.window.max_temp_spin.value())

    def test_each_rgb_drag_blocks_timer_and_backend_until_release(self):
        lightbar = self.window.lightbar_window
        self.window._lightbar_available = True
        lightbar.show_state({'available': True, 'mode': 'color', 'color': [1,2,3]})
        with patch('frontend.fan_control_gui.LIGHTBAR_APPLY_DELAY_MS', 20):
            for index, slider in enumerate(lightbar.color_sliders):
                with self.subTest(index=index):
                    self.backend.reset_mock()
                    slider.triggerAction(QAbstractSlider.SliderAction.SliderSingleStepAdd)
                    self.assertTrue(lightbar._apply_timer.isActive())
                    slider.setSliderDown(True)
                    self.assertFalse(lightbar._apply_timer.isActive())
                    slider.setSliderPosition(20 + index)
                    QTest.qWait(40)
                    lightbar._emit_apply_if_released(); self.window.apply_lightbar()
                    self.backend.request.assert_not_called()
                    slider.setSliderDown(False); QTest.qWait(40)
                    color = [s.value() for s in lightbar.color_sliders]
                    self.backend.request.assert_called_once_with('set_lightbar', mode='color', red=color[0], green=color[1], blue=color[2])

    def test_gpu_power_still_applies_only_after_release(self):
        self.window._gpu_power_available = True; self.window._gpu_power_applied = 0
        slider=self.window.gpu_power_slider
        slider.triggerAction(QAbstractSlider.SliderAction.SliderSingleStepAdd)
        slider.setSliderDown(True); slider.setSliderPosition(37)
        QTest.qWait(40); self.window.apply_gpu_power()
        self.backend.request.assert_not_called()
        slider.setSliderDown(False); QTest.qWait(40)
        self.backend.request.assert_called_once_with('set_gpu_power', offset=35)

    def test_real_keyboard_and_wheel_events_still_commit_without_dragging(self):
        from PySide6.QtCore import QPoint, QPointF
        from PySide6.QtGui import QWheelEvent
        self.manual_mode()
        slider = self.window.cpu_slider
        QTest.keyClick(slider, Qt.Key.Key_Right)
        QTest.qWait(40)
        self.backend.request.assert_called_once_with('apply_manual', cpu=55, gpu=50, confirmed_low=False)
        self.backend.reset_mock()
        wheel = QWheelEvent(QPointF(5,5), QPointF(5,5), QPoint(0,0), QPoint(0,120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        self.app.sendEvent(slider, wheel)
        QTest.qWait(40)
        self.assertGreater(slider.value(), 55)
        self.backend.request.assert_called_once_with('apply_manual', cpu=slider.value(), gpu=50, confirmed_low=False)

    def test_temperature_spin_focus_out_cannot_start_a_write_before_drag(self):
        self.window.min_temp_spin.editingFinished.emit()
        self.assertTrue(self.window._auto_configure_timer.isActive())
        slider=self.window.min_temp_slider
        slider.setSliderDown(True); slider.setSliderPosition(44)
        QTest.qWait(40)
        self.backend.request.assert_not_called()
        slider.setSliderDown(False); QTest.qWait(40)
        self.backend.request.assert_called_once_with('configure_auto', minimum_temp=44, maximum_temp=self.window.max_temp_spin.value())
