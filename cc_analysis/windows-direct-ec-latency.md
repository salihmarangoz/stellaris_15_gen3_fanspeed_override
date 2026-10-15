# Windows Direct EC command latency

Observed and tested 2026-10-16, Europe/Istanbul (UTC+03:00). The time at which the original performance problem first arose is unknown.

## Cause and change

Direct EC used a socket connection to IPv6 ::1:13688 to check OEM ownership before each byte write. With the broker stopped, five read-only checks measured 221.3, 201.4, 201.5, 201.0 and 201.4 ms. A complete fan write performs about 100 byte writes, so these checks added roughly 20 seconds before other work. The controller's shared locks could make Boost wait behind a full update.

The Windows path now reads the local IPv6 listener table using `GetExtendedTcpTable` from System32 `iphlpapi.dll`, class TCP_TABLE_OWNER_PID_LISTENER. A listener at ::1 or :: on the OEM port counts as ownership. The function binding is cached, but ownership results are never cached. Buffer growth is retried with bounded allocation; API errors and malformed/truncated tables raise, preventing EC writes. Every original per-byte guard, including rollback, remains in place. No EC address, curve format, write order, sensor source, lock or MUX write behavior changed. Non-Windows socket probing is unchanged.

Sources: [Microsoft API](https://learn.microsoft.com/en-us/windows/win32/api/iphlpapi/nf-iphlpapi-getextendedtcptable), [IPv6 row layout and port byte order](https://learn.microsoft.com/en-us/windows/win32/api/tcpmib/ns-tcpmib-mib_tcp6row_owner_pid).

## Explicitly authorized live test

The owner offered to exit the GUI, permitted Manual tests, prohibited targets below 50% on either fan, and monitored temperatures. The GUI was confirmed exited. An ignored elevated harness constructed the real backend controller in Manual without starting Auto, peripheral maintenance or frontend-watchdog threads. An additional per-byte test guard rejected any CPU/GPU duty-table byte below raw 100 (50%). No GPU-power, lightbar or firmware settings were commanded.

The new ownership check averaged 0.107 ms over 100 calls (maximum 0.904 ms). A temporary listener on a separate ephemeral loopback port was detected while listening and absent after close. No OEM service was started for this check.

| Command | Completion time | EC byte writes |
| --- | ---: | ---: |
| Manual CPU/GPU 75% / 75% | 3256 ms | 104 |
| Manual CPU/GPU 50% / 50% | 3333 ms | 104 |
| Manual CPU/GPU 70% / 70% | 3223 ms | 104 |
| Fan Boost on | 88 ms | 4 |
| Fan Boost off, Manual | 69 ms | 4 |
| Manual CPU/GPU 60% / 80% | 3281 ms | 104 |
| Finalize CPU/GPU 100% / 100% | 3266 ms | full verified update |

Table readback confirmed every target zone stayed at or above 50%; no rejected low write occurred. Samples during the test were around CPU 40-41 C and GPU 39 C. The test finished with both tables at 100%. Firmware fan ramping and RPM response may lag command completion. The remaining full-update time includes driver I/O, backup and verification; it has not been decomposed into separate costs. Boost still waits behind an in-progress transaction. Sustained Auto, stress load and shutdown timing were not tested in this run.

Local raw results and the authorized harness remain ignored under build/qa/windows-fan-latency-live.json and build/qa/test-windows-fan-latency.py. They are not redistributed OEM binaries or firmware dumps.
