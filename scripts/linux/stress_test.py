#!/usr/bin/env python3
"""CPU + GPU stress test that verifies the running fan-control service.

Run as the user allowed to control the service (not root) inside the desktop
session, with a Python that has PySide6 (the installed GUI environment):

    /opt/stellaris-fan-control/.venv/bin/python scripts/linux/stress_test.py

It loads every CPU core (stress-ng when installed) and the NVIDIA GPU (an
offscreen OpenGL shader through PRIME render offload), samples the service
telemetry every two seconds, writes a CSV report, and stops both loads at once
if a temperature limit is crossed. It never writes fan settings itself.
"""

import argparse
import csv
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.fan_control_common import MAX_SAFE_AUTO_TEMP  # noqa: E402
from shared.fan_control_ipc import BackendClient  # noqa: E402


CPU_ABORT_C = 96.0
GPU_ABORT_C = 90.0
SAMPLE_INTERVAL_SECONDS = 2.0
FULL_SPEED_DEADLINE_SECONDS = 60.0
FBO_SIZE = 2048
PRIME_OFFLOAD_ENVIRONMENT = {
    "__NV_PRIME_RENDER_OFFLOAD": "1",
    "__GLX_VENDOR_LIBRARY_NAME": "nvidia",
    "__VK_LAYER_NV_optimus": "NVIDIA_only",
}
VERTEX_SHADER = """
#version 130
out vec2 uv;
void main() {
    vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
    uv = p;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""
FRAGMENT_SHADER = """
#version 130
uniform float t;
in vec2 uv;
out vec4 color;
void main() {
    vec2 c = uv * 3.0 - vec2(2.0, 1.5) + vec2(sin(t) * 1e-4, 0.0);
    vec2 z = vec2(0.0);
    float n = 0.0;
    for (int i = 0; i < 4000; i++) {
        z = vec2(z.x * z.x - z.y * z.y, 2.0 * z.x * z.y) + c;
        z = dot(z, z) > 4.0 ? vec2(0.0) : z;
        n += z.x;
    }
    color = vec4(fract(n), 0.0, 0.0, 1.0);
}
"""


def gpu_worker() -> int:
    from PySide6.QtGui import QGuiApplication, QOffscreenSurface, QOpenGLContext
    from PySide6.QtOpenGL import (
        QOpenGLFramebufferObject,
        QOpenGLShader,
        QOpenGLShaderProgram,
    )

    application = QGuiApplication([])
    context = QOpenGLContext()
    if not context.create():
        print("GPU load: cannot create an OpenGL context", flush=True)
        return 2
    surface = QOffscreenSurface()
    surface.setFormat(context.format())
    surface.create()
    context.makeCurrent(surface)
    functions = context.functions()
    renderer = str(functions.glGetString(0x1F01))
    print(f"GPU load renderer: {renderer}", flush=True)
    if "nvidia" not in renderer.lower():
        print("GPU load: PRIME offload did not select the NVIDIA GPU", flush=True)
        return 3
    framebuffer = QOpenGLFramebufferObject(FBO_SIZE, FBO_SIZE)
    framebuffer.bind()
    program = QOpenGLShaderProgram()
    program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, VERTEX_SHADER)
    program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, FRAGMENT_SHADER)
    if not program.link():
        print(f"GPU load: shader link failed: {program.log()}", flush=True)
        return 4
    program.bind()
    time_location = program.uniformLocation("t")
    functions.glViewport(0, 0, FBO_SIZE, FBO_SIZE)
    frame = 0
    while True:
        program.setUniformValue1f(time_location, float(frame))
        functions.glDrawArrays(0x0004, 0, 3)
        functions.glFinish()
        frame += 1
    del application


def cpu_worker() -> None:
    value = 0.0
    while True:
        for index in range(1, 200000):
            value += (index * 1.000001) ** 0.5
        value %= 1e6


def start_cpu_load(duration: int) -> list[Any]:
    if shutil.which("stress-ng"):
        process = subprocess.Popen(
            ["stress-ng", "--cpu", "0", "--cpu-method", "matrixprod", "--timeout", f"{duration}s"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        print("CPU load: stress-ng matrixprod on every core")
        return [process]
    workers = [
        multiprocessing.Process(target=cpu_worker, daemon=True)
        for _ in range(os.cpu_count() or 1)
    ]
    for worker in workers:
        worker.start()
    print(f"CPU load: {len(workers)} Python workers")
    return workers


def start_gpu_load() -> subprocess.Popen[str] | None:
    environment = {**os.environ, **PRIME_OFFLOAD_ENVIRONMENT}
    environment.pop("QT_QPA_PLATFORM", None)
    process = subprocess.Popen(
        [sys.executable, __file__, "--gpu-worker"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    line = process.stdout.readline().strip() if process.stdout else ""
    print(line or "GPU load: no output from the worker")
    time.sleep(1.0)
    if process.poll() is not None:
        remainder = process.stdout.read().strip() if process.stdout else ""
        print(f"GPU load stopped early {remainder}".strip())
        return None
    return process


def stop_loads(cpu: list[Any], gpu: subprocess.Popen[str] | None) -> None:
    for process in cpu + ([gpu] if gpu else []):
        try:
            if isinstance(process, subprocess.Popen):
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            else:
                process.terminate()
                process.join(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
            pass


def sample(client: BackendClient) -> dict[str, Any]:
    result = client.request("read_telemetry", request_timeout=20.0)
    temperatures = result.get("temperatures") or {}
    telemetry = result.get("telemetry") or {}
    backend = result.get("backend") or {}
    return {
        "cpu_c": temperatures.get("cpu_c"),
        "gpu_c": temperatures.get("gpu_c"),
        "cpu_w": temperatures.get("cpu_power_w"),
        "gpu_w": temperatures.get("gpu_power_w"),
        "cpu_fan": telemetry.get("CpuFanDuty"),
        "gpu_fan": telemetry.get("GpuFanDuty"),
        "cpu_rpm": telemetry.get("CpuFanRpm"),
        "gpu_rpm": telemetry.get("GpuFanRpm"),
        "automatic": backend.get("automatic"),
        "target": backend.get("auto_target"),
        "auto_error": backend.get("auto_error"),
        "emergency": backend.get("sensor_emergency"),
        "sensor_error": result.get("temperature_error") or backend.get("sensor_error"),
        "fan_fault": telemetry.get("FanAbnormal"),
    }


def fmt(value: Any, spec: str = ".1f") -> str:
    return "--" if value is None else format(value, spec)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--duration", type=int, default=180)
    parser.add_argument("--cooldown", type=int, default=30)
    parser.add_argument("--no-gpu", action="store_true")
    parser.add_argument("--gpu-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.gpu_worker:
        return gpu_worker()
    if os.geteuid() == 0:
        print("Run as your normal user, not root.")
        return 2

    client = BackendClient(timeout=20.0)
    try:
        first = sample(client)
    except Exception as exc:
        print(f"Cannot read the fan-control service: {exc}")
        return 2
    if not first["automatic"]:
        print("The service is in Manual mode; switch to Automatic before the test.")
        return 2

    report = Path.cwd() / f"stress-{datetime.now():%Y%m%d-%H%M%S}.csv"
    rows: list[dict[str, Any]] = []
    cpu_load: list[Any] = []
    gpu_load = None
    aborted = None
    started = time.monotonic()
    phase = "load"
    print(f"Stress test: {args.duration} s load + {args.cooldown} s cooldown. Ctrl+C stops it.")
    try:
        cpu_load = start_cpu_load(args.duration)
        gpu_load = None if args.no_gpu else start_gpu_load()
        print(" time phase   CPU C  GPU C  CPU W  GPU W  target  fanCPU fanGPU   RPM CPU/GPU  notes")
        while True:
            elapsed = time.monotonic() - started
            if phase == "load" and elapsed >= args.duration:
                stop_loads(cpu_load, gpu_load)
                phase = "cool"
            if elapsed >= args.duration + args.cooldown:
                break
            try:
                row = sample(client)
            except Exception as exc:
                row = {"auto_error": f"service request failed: {exc}"}
            row.update({"time": round(elapsed, 1), "phase": phase})
            rows.append(row)
            notes = "; ".join(
                str(note)
                for note in (
                    row.get("auto_error"),
                    "SENSOR EMERGENCY" if row.get("emergency") else None,
                    row.get("sensor_error"),
                    "FAN FAULT" if row.get("fan_fault") else None,
                )
                if note
            )
            print(
                f"{elapsed:5.0f} {phase:5s} {fmt(row.get('cpu_c')):>6} {fmt(row.get('gpu_c')):>6} "
                f"{fmt(row.get('cpu_w')):>6} {fmt(row.get('gpu_w')):>6} {fmt(row.get('target'), 'd'):>6}% "
                f"{fmt(row.get('cpu_fan')):>6} {fmt(row.get('gpu_fan')):>6}  "
                f"{fmt(row.get('cpu_rpm'), 'd'):>5}/{fmt(row.get('gpu_rpm'), 'd'):<5}  {notes}",
                flush=True,
            )
            cpu_c = row.get("cpu_c") or 0
            gpu_c = row.get("gpu_c") or 0
            if phase == "load" and (cpu_c >= CPU_ABORT_C or gpu_c >= GPU_ABORT_C):
                aborted = f"temperature limit crossed (CPU {cpu_c} C, GPU {gpu_c} C)"
                print(f"ABORT: {aborted}; stopping the load now")
                stop_loads(cpu_load, gpu_load)
                phase = "cool"
                started = time.monotonic() - args.duration
            time.sleep(SAMPLE_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        aborted = "interrupted"
    finally:
        stop_loads(cpu_load, gpu_load)

    with report.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=sorted({key for row in rows for key in row}))
        writer.writeheader()
        writer.writerows(rows)

    load_rows = [row for row in rows if row.get("phase") == "load"]
    failures: list[str] = []
    errors = sorted({str(row["auto_error"]) for row in rows if row.get("auto_error")})
    if errors:
        failures.append("service reported errors: " + " | ".join(errors))
    if any(row.get("emergency") for row in rows):
        failures.append("the sensor emergency triggered")
    hot = [row for row in load_rows if max(row.get("cpu_c") or 0, row.get("gpu_c") or 0) >= MAX_SAFE_AUTO_TEMP]
    if hot:
        first_hot = hot[0]["time"]
        full = [
            row for row in load_rows
            if row["time"] >= first_hot
            and min(row.get("cpu_fan") or 0, row.get("gpu_fan") or 0) >= 95
        ]
        if not full or full[0]["time"] - first_hot > FULL_SPEED_DEADLINE_SECONDS:
            failures.append(
                f"fans did not reach 95% within {FULL_SPEED_DEADLINE_SECONDS:.0f} s of {MAX_SAFE_AUTO_TEMP} C"
            )
    lagging = [
        row for row in load_rows
        if row["time"] >= 30 and row.get("target") is not None and row.get("cpu_fan") is not None
        and row["cpu_fan"] < row["target"] - 15
    ]
    if len(lagging) > 5:
        failures.append(f"CPU fan lagged the Auto target by more than 15% in {len(lagging)} samples")

    def peak(key: str) -> str:
        values = [row[key] for row in rows if isinstance(row.get(key), (int, float))]
        return fmt(max(values)) if values else "--"

    print()
    print(f"Report: {report}")
    print(
        f"Peaks: CPU {peak('cpu_c')} C / {peak('cpu_w')} W, GPU {peak('gpu_c')} C / {peak('gpu_w')} W, "
        f"fans {peak('cpu_fan')}% / {peak('gpu_fan')}%, RPM {peak('cpu_rpm')} / {peak('gpu_rpm')}"
    )
    if gpu_load is None and not args.no_gpu:
        failures.append("the GPU load could not start")
    if aborted:
        print(f"Stopped early: {aborted}")
    if failures:
        print("RESULT: FAIL")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("RESULT: PASS (Auto followed the load, no service errors, no sensor emergency)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
