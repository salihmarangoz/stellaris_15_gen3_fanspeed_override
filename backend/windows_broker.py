"""Read the Windows IPv6 listener table without a timed TCP connection."""

import ctypes
import socket
import struct
from functools import lru_cache
from typing import Any


_AF_INET6_WINDOWS = 23
_TCP_TABLE_OWNER_PID_LISTENER = 3
_ERROR_INSUFFICIENT_BUFFER = 122
_TCP_LISTEN = 2
_MAX_TABLE_BYTES = 16 * 1024 * 1024
_ROW = struct.Struct("<16sII16sIIII")


@lru_cache(maxsize=1)
def _table_api() -> Any:
    # Cache only the function binding, never a broker-absent result. Load from
    # System32 so unrelated application/tool DLL directories cannot win.
    library = ctypes.WinDLL("iphlpapi.dll", winmode=0x00000800)
    query = library.GetExtendedTcpTable
    query.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int,
        ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32,
    ]
    query.restype = ctypes.c_uint32
    return query


def _listener_table() -> bytes:
    query = _table_api()
    capacity = 4096
    for _ in range(3):
        buffer = ctypes.create_string_buffer(capacity)
        size = ctypes.c_uint32(capacity)
        status = query(
            buffer, ctypes.byref(size), False, _AF_INET6_WINDOWS,
            _TCP_TABLE_OWNER_PID_LISTENER, 0,
        )
        if status == _ERROR_INSUFFICIENT_BUFFER:
            capacity = max(capacity * 2, size.value)
            if capacity > _MAX_TABLE_BYTES:
                raise RuntimeError("Windows TCP listener table is unexpectedly large")
            continue
        if status != 0:
            raise RuntimeError(f"Cannot check OEM broker ownership (Windows error {status})")
        if not 4 <= size.value <= capacity:
            raise RuntimeError("Windows returned an invalid TCP listener table size")
        return buffer.raw[:size.value]
    raise RuntimeError("Windows TCP listener table kept changing; refusing EC access")


def _has_listener(table: bytes, host: str, port: int) -> bool:
    if len(table) < 4:
        raise RuntimeError("Windows returned a truncated TCP listener table")
    count = struct.unpack_from("<I", table)[0]
    if count > (len(table) - 4) // _ROW.size:
        raise RuntimeError("Windows returned truncated TCP listener rows")
    address = socket.inet_pton(socket.AF_INET6, host)
    for index in range(count):
        local, _, local_port, _, _, _, state, _ = _ROW.unpack_from(
            table, 4 + index * _ROW.size
        )
        # Ports occupy the first two bytes of a DWORD, in network byte order.
        decoded_port = int.from_bytes(local_port.to_bytes(4, "little")[:2], "big")
        if state == _TCP_LISTEN and decoded_port == port and local in (address, bytes(16)):
            return True
    return False


def broker_listening(host: str, port: int) -> bool:
    """Raise on an unknown result so callers cannot write on a failed check."""
    return _has_listener(_listener_table(), host, port)
