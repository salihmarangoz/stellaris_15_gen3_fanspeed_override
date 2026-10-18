import subprocess
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.direct_fan_control import CTGP_ADDRESSES, DirectEcClient
from backend.fan_control_service import ControlCenterService, load_gpu_power_choice
from backend.temperature_service import read_nvidia_power_limits


class NvidiaLimitsTests(unittest.TestCase):
    def test_bounded_read_only_query_and_malformed_limits(self):
        limits = {'base_limit_w': 115, 'max_limit_w': 165, 'enforced_limit_w': 120}
        with patch('backend.temperature_service._telemetry_helper.query', return_value=limits) as query:
            self.assertEqual(read_nvidia_power_limits(), {
                'base_limit_w': 115, 'max_limit_w': 165, 'enforced_limit_w': 120,
            })
            query.assert_called_once_with('limits', 2)
        for result in ({}, {'powered_off': True}, dict(limits, base_limit_w=None),
                       dict(limits, base_limit_w=float('nan')), dict(limits, base_limit_w=165, max_limit_w=115),
                       dict(limits, enforced_limit_w=166), dict(limits, max_limit_w=301),
                       dict(limits, enforced_limit_w='120'), dict(limits, base_limit_w=True)):
            with self.subTest(result=result), patch('backend.temperature_service._telemetry_helper.query',
                                                    return_value=result):
                with self.assertRaises((ValueError, RuntimeError)):
                    read_nvidia_power_limits()


class GpuDisplayTests(unittest.TestCase):
    def test_warning_for_driver_limit_above_or_below_selected_range(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from frontend.fan_control_gui import FanControlWindow
        for enforced, boost, expected in ((100, False, 'warning'), (115, False, 'ok'),
                                         (120, False, 'warning'), (140, True, 'ok'),
                                         (165, True, 'warning')):
            with self.subTest(enforced=enforced, boost=boost):
                window = SimpleNamespace(
                    _gpu_power_state={'available': True, 'offset': 0, 'max_limit_w': 165,
                                      'enforced_limit_w': enforced, 'dynamic_boost': boost,
                                      'dynamic_boost_w': 25},
                    _gpu_power_base=115, gpu_power_slider=Mock(),
                    _gpu_power_apply_timer=Mock(), gpu_power_max_label=Mock(),
                    gpu_power_equation_label=Mock(), _gpu_limit_is_fresh=lambda: True,
                    _set_limit_badge=Mock(), _set_label_warning=Mock(),
                )
                window.gpu_power_slider.value.return_value = 0
                window.gpu_power_slider.isSliderDown.return_value = False
                window._gpu_power_apply_timer.isActive.return_value = False
                FanControlWindow._update_gpu_power_text(window)
                self.assertEqual(window._set_limit_badge.call_args.args[1], expected)

    def test_boost_term_shows_current_boost_out_of_available_room(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from frontend.fan_control_gui import FanControlWindow
        for enforced, boost, expected in (
            (115, True, '115 W sustained + 0 of 25 W boost ='),
            (140, True, '115 W sustained + 25 of 25 W boost ='),
            (114.5, True, '115 W sustained + 0 of 25 W boost ='),
            (115, False, '115 W sustained + 0 W boost ='),
        ):
            with self.subTest(enforced=enforced, boost=boost):
                window = SimpleNamespace(
                    _gpu_power_state={'available': True, 'offset': 0, 'max_limit_w': 165,
                                      'enforced_limit_w': enforced, 'dynamic_boost': boost,
                                      'dynamic_boost_w': 25},
                    _gpu_power_base=115, gpu_power_slider=Mock(),
                    _gpu_power_apply_timer=Mock(), gpu_power_max_label=Mock(),
                    gpu_power_equation_label=Mock(), _gpu_limit_is_fresh=lambda: True,
                    _set_limit_badge=Mock(), _set_label_warning=Mock(),
                )
                window.gpu_power_slider.value.return_value = 0
                window.gpu_power_slider.isSliderDown.return_value = False
                window._gpu_power_apply_timer.isActive.return_value = False
                FanControlWindow._update_gpu_power_text(window)
                window.gpu_power_equation_label.setText.assert_called_once_with(expected)


class GpuSliderTests(unittest.TestCase):
    def test_drag_cancels_pending_apply_and_release_sends_only_final_value(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication, QAbstractSlider
        from frontend.fan_control_gui import FanControlWindow

        app = QApplication.instance() or QApplication([])
        with (
            TemporaryDirectory() as directory,
            patch.object(FanControlWindow, '_load_initial_state'),
            patch.object(FanControlWindow, '_settings_path',
                         return_value=Path(directory) / 'settings.json'),
        ):
            backend = Mock()
            window = FanControlWindow(backend=backend)
            window._run = Mock(side_effect=lambda operation, *args, **kwargs: operation())
            window._gpu_power_available = True
            window._gpu_power_applied = 0
            window._gpu_power_apply_timer.setInterval(25)
            slider = window.gpu_power_slider
            try:
                # An earlier key/click must not apply an intermediate drag value.
                slider.triggerAction(QAbstractSlider.SliderAction.SliderSingleStepAdd)
                self.assertTrue(window._gpu_power_apply_timer.isActive())
                slider.setSliderDown(True)
                self.assertFalse(window._gpu_power_apply_timer.isActive())
                for value in (10, 20, 35):
                    slider.setSliderPosition(value)
                    QTest.qWait(35)
                # Even a queued timeout must refuse while the handle is held.
                window.apply_gpu_power()
                backend.request.assert_not_called()
                slider.setSliderDown(False)
                QTest.qWait(50)
                backend.request.assert_called_once_with('set_gpu_power', offset=35)

                # Keyboard control still applies after its debounce interval.
                backend.request.reset_mock()
                slider.triggerAction(QAbstractSlider.SliderAction.SliderSingleStepAdd)
                QTest.qWait(50)
                backend.request.assert_called_once_with('set_gpu_power', offset=40)
            finally:
                window._closing = True
                window.tray_icon.hide()
                window.deleteLater()
                app.processEvents()


class DirectGpuPowerTests(unittest.TestCase):
    def setUp(self):
        self.registers = {0x740: 0x10, 0x78E: 0x40, 0x743: 0x80, 0x744: 0,
                          0x745: 0, 0x746: 0, 0x7C5: 0xA0}
        self.original = dict(self.registers)
        self.writes = []
        self.broker = Mock(return_value=False)
        self.client = DirectEcClient(broker_available=self.broker)
        self.client._read_ec = lambda address: self.registers[address]
        self.client._write_ec = self.write
        platform = patch('backend.direct_fan_control.validate_ctgp_platform')
        self.platform = platform.start()
        self.addCleanup(platform.stop)

    def write(self, address, value):
        self.writes.append((address, value))
        self.registers[address] = value

    def test_data_before_control_and_preserves_unrelated_and_fan_bytes(self):
        self.client.write_ctgp(25, True)
        self.assertEqual(self.writes, [(0x744, 25), (0x745, 255), (0x746, 25), (0x743, 0x87)])
        self.assertEqual(self.registers[0x7C5], 0xA0)
        state = self.client.read_ctgp_state()
        self.assertEqual((state['offset'], state['dynamic_boost'], state['configured']), (25, True, True))
        self.writes.clear()
        self.client.write_ctgp(25, False)
        self.assertEqual(self.writes, [(0x743, 0x85)])
        self.writes.clear()
        self.client.write_ctgp(25, False)
        self.assertEqual(self.writes, [])

    def test_refuses_wrong_platform_flags_and_malformed_configuration(self):
        for address, value in ((0x740, 0x11), (0x78E, 0), (0x744, 51), (0x746, 26)):
            with self.subTest(address=address):
                self.registers = dict(self.original)
                self.registers[address] = value
                with self.assertRaises(RuntimeError):
                    self.client.write_ctgp(0, False)
        self.registers = dict(self.original)
        self.platform.side_effect = RuntimeError('wrong BIOS')
        with self.assertRaisesRegex(RuntimeError, 'BIOS'):
            self.client.write_ctgp(0, False)
        self.assertEqual(self.writes, [])

    def test_refuses_bad_input_and_oem_before_writing(self):
        for offset, boost in ((True, True), (1.5, False), (-1, False), (51, False), (25, 1)):
            with self.assertRaises(ValueError):
                self.client.write_ctgp(offset, boost)
        self.broker.return_value = True
        with self.assertRaisesRegex(RuntimeError, 'GCUBridge'):
            self.client.write_ctgp(25, False)
        self.assertEqual(self.writes, [])

    def test_verification_failure_restores_only_gpu_bytes(self):
        def fail_once(address, value):
            self.writes.append((address, value))
            if len(self.writes) != 2:
                self.registers[address] = value
        self.client._write_ec = fail_once
        with self.assertRaisesRegex(RuntimeError, 'readback'):
            self.client.write_ctgp(25, True)
        self.assertEqual(self.registers, self.original)
        self.assertTrue(all(address in CTGP_ADDRESSES for address, value in self.writes))

    def test_oem_return_refuses_further_write_and_rollback(self):
        self.broker.side_effect = lambda: bool(self.writes)
        with self.assertRaisesRegex(RuntimeError, 'rollback could not complete'):
            self.client.write_ctgp(25, True)
        self.assertEqual(self.writes, [(0x744, 25)])


class FakeGpuClient:
    method_name = 'direct_ec'

    def __init__(self):
        self.state = {'offset': 0, 'dynamic_boost': False, 'dynamic_boost_w': 0, 'configured': False}
        self.writes = []

    def read_ctgp_state(self):
        return dict(self.state)

    def write_ctgp(self, offset, boost):
        self.writes.append((offset, boost))
        self.state.update(offset=offset, dynamic_boost=boost, dynamic_boost_w=25, configured=True)


class GpuServiceTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'gpu-power.json'
        for name, options in (
            ('gpu_power_settings_path', {'return_value': self.path}),
            ('load_lightbar_choice', {'return_value': None}),
            ('oem_broker_available', {'return_value': False}),
            ('read_nvidia_power_limits', {'return_value': {
                'base_limit_w': 115.0, 'max_limit_w': 165.0, 'enforced_limit_w': 120.0}}),
        ):
            patcher = patch('backend.fan_control_service.' + name, **options)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        self.service = object.__new__(ControlCenterService)
        self.service.__init__()
        self.client = FakeGpuClient()
        self.service._method = 'direct_ec'
        self.service._connected_client = Mock(return_value=self.client)

    def test_read_and_startup_do_not_write_until_user_chooses(self):
        state = self.service.gpu_power_state()
        self.assertTrue(state['available'])
        self.assertEqual(state['enforced_limit_w'], 120)
        self.service.maintain_gpu_power()
        self.assertEqual(self.client.writes, [])
        self.assertFalse(self.path.exists())

    def test_selection_persists_and_maintenance_repairs_drift(self):
        self.service.set_gpu_power_offset(25)
        self.assertEqual(load_gpu_power_choice(), (25, False))
        self.service.set_dynamic_boost(True)
        self.assertEqual(load_gpu_power_choice(), (25, True))
        self.service.maintain_gpu_power()
        self.assertEqual(self.client.writes, [(25, False), (25, True)])
        self.client.state['offset'] = 0
        self.service.maintain_gpu_power()
        self.assertEqual(self.client.writes[-1], (25, True))
        restored = object.__new__(ControlCenterService)
        restored.__init__()
        self.assertEqual(restored._desired_gpu_power, (25, True))

    def test_zero_choice_restores_enable_after_firmware_reset(self):
        self.service._desired_gpu_power = (0, False)
        self.service.maintain_gpu_power()
        self.assertEqual(self.client.writes, [(0, False)])

    def test_oem_and_missing_or_unsupported_limits_refuse_writes(self):
        self.oem_broker_available.return_value = True
        self.assertFalse(self.service.gpu_power_state()['available'])
        with self.assertRaisesRegex(RuntimeError, 'GCUBridge'):
            self.service.set_gpu_power_offset(25)
        self.service._desired_gpu_power = (25, True)
        self.service.maintain_gpu_power()
        self.assertEqual(self.client.writes, [])
        self.oem_broker_available.return_value = False
        for limits in ({'base_limit_w': 100, 'max_limit_w': 165, 'enforced_limit_w': 120},
                       {'base_limit_w': 115, 'max_limit_w': 140, 'enforced_limit_w': 120}):
            self.read_nvidia_power_limits.return_value = limits
            self.service._gpu_limits = None
            with self.assertRaises((RuntimeError, ValueError)):
                self.service.set_gpu_power_offset(50)
        self.assertEqual(self.client.writes, [])
        self.read_nvidia_power_limits.side_effect = subprocess.TimeoutExpired('nvidia-smi', 2)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.service.set_gpu_power_offset(25)
        self.assertEqual(self.client.writes, [])

    def test_failed_write_does_not_save_or_retry(self):
        self.client.write_ctgp = Mock(side_effect=RuntimeError('readback failed'))
        with self.assertRaisesRegex(RuntimeError, 'readback'):
            self.service.set_gpu_power_offset(25)
        self.assertFalse(self.path.exists())
        self.assertIsNone(self.service._desired_gpu_power)
        self.client.write_ctgp.assert_called_once_with(25, False)

    def test_bad_settings_are_ignored(self):
        for text in ('broken', '[]', '{"offset":51,"dynamic_boost":false}',
                     '{"offset":true,"dynamic_boost":false}', '{"offset":25,"dynamic_boost":1}'):
            self.path.write_text(text)
            self.assertIsNone(load_gpu_power_choice())


if __name__ == '__main__':
    unittest.main()
