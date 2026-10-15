import ctypes
import socket
import struct
import unittest
from unittest.mock import Mock, patch

from backend import windows_broker as broker
from backend.direct_fan_control import DirectEcClient, oem_broker_available


def row(host="::1", port=13688, state=2):
    network_port = int.from_bytes(port.to_bytes(2, "big") + bytes(2), "little")
    return broker._ROW.pack(
        socket.inet_pton(socket.AF_INET6, host), 0, network_port,
        bytes(16), 0, 0, state, 1234,
    )


def table(*rows):
    return struct.pack("<I", len(rows)) + b"".join(rows)


class WindowsBrokerTests(unittest.TestCase):
    def test_loopback_and_wildcard_listeners_block_direct_writes(self):
        for host in ("::1", "::"):
            with self.subTest(host=host):
                self.assertTrue(broker._has_listener(table(row(host)), "::1", 13688))

    def test_other_ports_addresses_and_connections_are_not_the_broker(self):
        for entry in (row(port=13689), row(host="fe80::1"), row(state=5)):
            self.assertFalse(broker._has_listener(table(entry), "::1", 13688))
        self.assertFalse(broker._has_listener(table(), "::1", 13688))
        self.assertTrue(broker._has_listener(table(row(port=10), row()), "::1", 13688))

    def test_malformed_tables_refuse_an_absent_result(self):
        for data in (b"", bytes(3), table(row())[:-1], struct.pack("<I", 0xffffffff)):
            with self.subTest(data=data), self.assertRaises(RuntimeError):
                broker._has_listener(data, "::1", 13688)

    def test_table_query_retries_growth_and_uses_ipv6_listener_class(self):
        payload = table(row())
        calls = []

        def query(buffer, size_pointer, order, family, table_class, reserved):
            size = ctypes.cast(size_pointer, ctypes.POINTER(ctypes.c_uint32))
            calls.append((len(buffer), order, family, table_class, reserved))
            if len(calls) == 1:
                size.contents.value = 8192
                return 122
            ctypes.memmove(buffer, payload, len(payload))
            size.contents.value = len(payload)
            return 0

        with patch.object(broker, "_table_api", return_value=query):
            self.assertEqual(broker._listener_table(), payload)
        self.assertEqual(calls, [(4096, False, 23, 3, 0), (8192, False, 23, 3, 0)])

    def test_api_failure_invalid_size_or_unbounded_growth_refuses(self):
        def invalid_size(buffer, size, *args):
            ctypes.cast(size, ctypes.POINTER(ctypes.c_uint32)).contents.value = 1
            return 0

        def too_large(buffer, size, *args):
            ctypes.cast(size, ctypes.POINTER(ctypes.c_uint32)).contents.value = 2**31
            return 122

        for query in (Mock(return_value=5), invalid_size, too_large, Mock(return_value=122)):
            with self.subTest(query=query), patch.object(broker, "_table_api", return_value=query):
                with self.assertRaises(RuntimeError):
                    broker._listener_table()

    def test_windows_route_never_opens_a_tcp_connection_or_caches_absence(self):
        with patch("backend.direct_fan_control.os.name", "nt"), \
             patch.object(broker, "_listener_table", side_effect=[table(), table(row())]), \
             patch("backend.direct_fan_control.socket.create_connection") as connect:
            self.assertFalse(oem_broker_available())
            self.assertTrue(oem_broker_available())
        connect.assert_not_called()

    def test_unknown_check_refuses_the_actual_ec_write(self):
        client = DirectEcClient()
        client._write_ec = Mock()
        with patch("backend.direct_fan_control.os.name", "nt"), \
             patch.object(broker, "_listener_table", side_effect=RuntimeError("ownership unknown")):
            with self.assertRaisesRegex(RuntimeError, "ownership unknown"):
                client._write(0x741, 1)
        client._write_ec.assert_not_called()


if __name__ == "__main__":
    unittest.main()
