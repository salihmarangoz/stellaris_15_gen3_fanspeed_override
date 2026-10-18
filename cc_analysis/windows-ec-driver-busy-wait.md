# Windows EC driver busy-wait

Static analysis on 2026-10-18, Europe/Istanbul (UTC+03:00), of the installed files only. Nothing was loaded, called or patched. Copies for analysis live in ignored `build/oem-analysis/ec-driver/`.

| File | SHA-256 |
| --- | --- |
| `ACPIDriverDll.dll` (Control Center 4.9.47.13, the pinned image) | `ee2c506dd414222013e6dbe5c67a774b1fa1387a47ad5d705a82f048c1a945bb` |
| `C:\Windows\System32\drivers\UWACPIDriver.sys` (KMDF) | `e9840521975e9073881a488f9e978a18c93aed44c57fe2382b1d513fd27126e2` |

## How one EC byte is read

`ReadEC(address)` in the DLL stores the address as a dword at the start of a 4 MiB global buffer. It opens `\\.\ACPIDriver` with `CreateFileW` (read/write, share read/write), then calls `DeviceIoControl(0x9C40A488)` with the same buffer as input and output and **4 MiB (`0x400000`) for both lengths**. It then closes the handle and returns the buffer's first byte. `WriteEC(address, value)` is identical with IOCTL `0x9C40A48C`, the address dword at offset 0 and the value at offset 4. Every byte therefore costs a device open and close plus a 4 MiB METHOD_BUFFERED request.

In the driver, the IOCTL handler (`0x2870`) checks only that both lengths are non-zero. It then calls helper `0x281c` with the input buffer and **InputBufferLength** as the count. That helper is unoptimized code that loops `count` times, decrementing the counter and incrementing a local pointer, and stores nothing: it is a pure delay. With the DLL's 4 MiB input it runs 4,194,304 iterations per byte. Its loop body is exactly the `0x2832`-`0x2854` range that held 83% of the backend's kernel samples in the 2026-10-18 trace, and it accounts for the measured ~9 ms per byte.

The read case (`0x2130`) builds an `ACPI_EVAL_INPUT_BUFFER_COMPLEX` for method `ECRR` with one integer argument, copied from the first 4 input bytes. It sends `IOCTL_ACPI_EVAL_METHOD` (`0x32C004`) to the ACPI driver, checks the output signature `AeoB`, and copies the returned integer's data into the request's output buffer. The write case (`0x25b8`) evaluates `ECRW` with two integer arguments: the address dword from input offset 0 and one byte from offset 4. Both are the DSDT INOU methods that the Linux service reproduces through the `0xFE200000` EC RAM window, so ACPI.sys executes AML for every byte as well.

Other dispatch codes seen: `0x9C40A480`, `0x9C40A484`, `0x9C40A490` (`ReadMEMB`), `0x9C40A494`, `0x9C40A498`, `0x9C40A49C`. They were not analyzed further.

## Implication

The cost is a fixed delay chosen by the DLL's buffer size, not the EC handshake. Sending the same two IOCTLs directly, with a small buffer (for example 64 bytes, large enough for the returned integer) and one handle kept open, would run the same `ECRR`/`ECRW` methods with a 64-iteration delay instead of 4,194,304. Bypassing the DLL moves the supply-chain boundary from the DLL hash to the driver hash and the reverse-engineered request layout. It needs an elevated read-only comparison against `ReadEC` on the target before any write uses it.
