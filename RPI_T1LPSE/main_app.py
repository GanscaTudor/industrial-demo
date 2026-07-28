#!/usr/bin/env python3
"""
Industrial Demo Control Panel with ADXL355 Vibration Monitor
(color sensor variant: COLOR_READ polled at 4 Hz over IP)

Combined application integrating:
- APARD board control (Servo, TCS34725 color sensor)
- CN0575 temperature monitoring
- SWIOT1L fan PWM control
- ADXL355 predictive maintenance monitor

Requirements:
    pip3 install matplotlib pyadi-iio numpy

Usage:
    python3 main_app_color4hz.py                     # real hardware
    python3 main_app_color4hz.py --demo              # ADXL355 synthetic data
    python3 main_app_color4hz.py --adxl-host IP      # ADXL355 server address
"""

import argparse
import json
import socket
import struct
import threading
import time
from datetime import datetime
from collections import deque
import numpy as np
import tkinter as tk
from tkinter import ttk, scrolledtext

try:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False
    print("Warning: matplotlib not found. Graphs will be disabled.")
    print("Install with: pip3 install matplotlib")

try:
    import adi
    HAS_ADI = True
except ImportError:
    HAS_ADI = False
    print("Warning: pyadi-iio not found. SWIOT1L panel will be disabled.")
    print("Install with: pip3 install pyadi-iio")

# ---------------------------------------------------------------------------
# CLI arguments
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser()
parser.add_argument("--demo", action="store_true", help="Use synthetic ADXL355 data")
parser.add_argument("--adxl-host", type=str, default="localhost", help="ADXL355 server IP")
parser.add_argument("--adxl-port", type=int, default=50055, help="ADXL355 server port")
parser.add_argument("--adxl-rate", type=int, default=1000, help="ADXL355 sample rate")
parser.add_argument("--adxl-buf", type=int, default=2048, help="FFT window size")
parser.add_argument("--adxl-chunk", type=int, default=256, help="Samples per acquisition")
args = parser.parse_args()

# ---------------------------------------------------------------------------
# Network configuration
# ---------------------------------------------------------------------------
APARD1_IP = "192.168.98.50"
APARD2_IP = "192.168.98.60"
CN0575_IP = "192.168.10.2"
SWIOT_IP = "192.168.97.40"
TCP_PORT = 10000
TIMEOUT = 5.0
AUTO_REFRESH_MS = 5000
COLOR_AUTO_REFRESH_MS = 250  # 4 reads/sec over IP for the color sensor panel
GRAPH_MAX_POINTS = 60
DC_RPM = 4500
PWM_PERIOD = 0.01

# ADXL355 settings
ADXL_FS = args.adxl_rate
ADXL_FFT_WIN = args.adxl_buf
ADXL_CHUNK = args.adxl_chunk
ADXL_FREQ_MAX = min(500, ADXL_FS / 2)
M_S2_TO_G = 1.0 / 9.80665

# Predictive maintenance thresholds
THRESH_WARN = 0.10
THRESH_ALARM = 0.50
CREST_WARN = 4.0
KURT_WARN = 4.0

AXIS_NAMES = ["X", "Y", "Z"]
AXIS_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c"]
LEVEL_COLOR = {"OK": "#2ecc71", "WARNING": "#f39c12", "ALARM": "#e74c3c"}

# TCS34725 color sensor settings
COLOR_AXIS_NAMES = ["R", "G", "B"]
COLOR_AXIS_COLORS = ["#e74c3c", "#2ecc71", "#3498db"]

# Color-cube detection -> automatic servo triggering.
# Runs continuously in the background, independent of the "Live" display checkbox.
COLOR_CUBE_THRESHOLD = 500

RED_SERVO_DELAY_S = 0.0
RED_SERVO_ON_DURATION_S = 4.0

GREEN_SERVO_DELAY_S = 3.0
GREEN_SERVO_ON_DURATION_S = 4.0


# ---------------------------------------------------------------------------
# Utility: TCP command sender
# ---------------------------------------------------------------------------
def send_command(ip, cmd, timeout=TIMEOUT):
    """Open connection, send one command, read response, close."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((ip, TCP_PORT))
            s.sendall((cmd + "\n").encode("ascii"))
            data = s.recv(1024)
            if not data:
                return None
            return data.decode("ascii").strip()
    except (socket.error, OSError):
        return None


# ---------------------------------------------------------------------------
# ADXL355 Data Sources
# ---------------------------------------------------------------------------
class DemoSource:
    """Generates synthetic vibration: 50 Hz fundamental + harmonics + noise."""

    def __init__(self):
        self._t = 0.0
        self._fault = False
        self._fault_timer = 0

    def read_chunk(self, n):
        t = np.linspace(self._t, self._t + n / ADXL_FS, n, endpoint=False)
        self._t += n / ADXL_FS
        data = []
        for axis_i in range(3):
            freq = [50, 100, 150][axis_i]
            amp = [0.05, 0.03, 0.02][axis_i]
            sig = amp * np.sin(2 * np.pi * freq * t)
            sig += 0.01 * np.random.randn(n)
            if axis_i == 0:
                self._fault_timer += n
                if self._fault_timer > ADXL_FS * 8:
                    self._fault_timer = 0
                    self._fault = True
                if self._fault:
                    idx = np.random.randint(0, n)
                    sig[idx] += 0.8 * np.random.choice([-1, 1])
                    self._fault = False
            data.append(sig * 9.80665)
        return data


class TCPSource:
    """TCP client for adxl355_server.py."""

    def __init__(self, host, port):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.connect((host, port))
        self._sock.settimeout(10.0)
        cfg = json.loads(self._recv_msg())
        self.fs = cfg["fs"]
        self.chunk = cfg["chunk"]

    def _recvall(self, n):
        buf = bytearray()
        while len(buf) < n:
            pkt = self._sock.recv(n - len(buf))
            if not pkt:
                raise ConnectionError("Server closed connection")
            buf.extend(pkt)
        return bytes(buf)

    def _recv_msg(self):
        length = struct.unpack(">I", self._recvall(4))[0]
        return self._recvall(length)

    def read_chunk(self, _n):
        msg = self._recv_msg()
        arr = np.frombuffer(msg, dtype=np.float32).reshape(3, -1)
        return [arr[i] for i in range(3)]


class ADXL355Acquisition:
    """Threaded acquisition for ADXL355 data."""

    def __init__(self, source):
        self._source = source
        self._buf = [np.zeros(ADXL_FFT_WIN) for _ in range(3)]
        self._lock = threading.Lock()
        self._running = False
        self._thread = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def snapshot(self):
        with self._lock:
            return [b.copy() for b in self._buf]

    def _loop(self):
        while self._running:
            try:
                raw = self._source.read_chunk(ADXL_CHUNK)
            except Exception:
                time.sleep(0.1)
                continue
            with self._lock:
                for i in range(3):
                    chunk = np.asarray(raw[i], dtype=np.float64) * M_S2_TO_G
                    self._buf[i] = np.roll(self._buf[i], -len(chunk))
                    self._buf[i][-len(chunk):] = chunk


def compute_metrics(samples):
    """Compute RMS, peak, crest factor, kurtosis."""
    ac = samples - samples.mean()
    rms = float(np.sqrt(np.mean(ac ** 2)))
    peak = float(np.abs(ac).max())
    cf = peak / rms if rms > 1e-9 else 0.0
    std = ac.std()
    kurt = float(np.mean((ac / std) ** 4)) if std > 1e-9 else 3.0
    return rms, peak, cf, kurt


def health_level(rms, cf, kurt):
    """Determine health status from metrics."""
    if rms >= THRESH_ALARM or cf >= CREST_WARN * 1.5 or kurt >= KURT_WARN * 2:
        return "ALARM"
    if rms >= THRESH_WARN or cf >= CREST_WARN or kurt >= KURT_WARN:
        return "WARNING"
    return "OK"


# ---------------------------------------------------------------------------
# UI Panels
# ---------------------------------------------------------------------------
def rgb_counts_to_hex(r, g, b):
    """Normalize raw 16-bit ADC counts to a displayable sRGB swatch color."""
    peak = max(r, g, b, 1)
    scale = 255.0 / peak
    rr = min(int(r * scale), 255)
    gg = min(int(g * scale), 255)
    bb = min(int(b * scale), 255)
    return f"#{rr:02x}{gg:02x}{bb:02x}"


class ColorSensorPanel(ttk.LabelFrame):
    """UI panel for an APARD board with a TCS34725 color sensor.

    A background thread continuously polls COLOR_READ over IP at
    COLOR_AUTO_REFRESH_MS (4 Hz) regardless of the "Live" checkbox, and
    checks every reading for a color cube (see _check_cube_trigger). The
    "Live" checkbox only controls whether those readings are reflected in
    the swatch/labels/graph.
    """

    def __init__(self, parent, board_name, ip, log_callback, servo_panel=None):
        super().__init__(parent, text=f"  {board_name} ({ip})  ", padding=10)
        self.board_name = board_name
        self.ip = ip
        self.log = log_callback
        self.servo_panel = servo_panel
        self.rgb_history = [deque(maxlen=GRAPH_MAX_POINTS) for _ in range(3)]
        self.time_history = deque(maxlen=GRAPH_MAX_POINTS)
        self.start_time = None
        self.cal_gains = None
        self.auto_refresh_var = tk.BooleanVar(value=False)
        self._poll_running = True
        self._sequence_active = False
        # Tracks the color of whatever cube is currently sitting under the
        # sensor (None if nothing above threshold), plus whether that
        # specific physical cube has already been acted on / had its
        # "ignored, sequence busy" message logged - so a cube that gets
        # ignored while another sequence is running is retried on every
        # subsequent poll instead of being silently skipped forever.
        self._cube_color = None
        self._cube_handled = False
        self._cube_ignore_logged = False
        self._build_ui()
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _build_ui(self):
        status_frame = ttk.Frame(self)
        status_frame.pack(fill="x", pady=(0, 8))

        ttk.Label(status_frame, text="Status:").pack(side="left")
        self.status_label = ttk.Label(
            status_frame, text="Unknown", foreground="gray",
            font=("TkDefaultFont", 10, "bold")
        )
        self.status_label.pack(side="left", padx=6)
        ttk.Button(status_frame, text="Test", command=self._test_connection).pack(side="right")

        color_frame = ttk.LabelFrame(self, text="Color Sensor", padding=6)
        color_frame.pack(fill="x", pady=4)

        swatch_row = ttk.Frame(color_frame)
        swatch_row.pack(fill="x")

        self.swatch = tk.Canvas(swatch_row, width=80, height=50, bg="#000000",
                                 highlightthickness=1, highlightbackground="#888")
        self.swatch.pack(side="left", padx=(0, 8))
        self.swatch_rect = self.swatch.create_rectangle(0, 0, 80, 50, fill="#000000", outline="")

        info_col = ttk.Frame(swatch_row)
        info_col.pack(side="left", fill="both", expand=True)

        self.rgb_label = ttk.Label(info_col, text="R: -   G: -   B: -",
                                    font=("Courier", 10, "bold"))
        self.rgb_label.pack(anchor="w")
        self.hex_label = ttk.Label(info_col, text="#------", font=("Courier", 9))
        self.hex_label.pack(anchor="w")

        ctrl_row = ttk.Frame(color_frame)
        ctrl_row.pack(fill="x", pady=(6, 0))

        ttk.Button(ctrl_row, text="Read Color", command=self._read_color).pack(side="left", padx=2)
        ttk.Checkbutton(
            ctrl_row, text="Live (4Hz)",
            variable=self.auto_refresh_var,
            command=self._toggle_auto_refresh
        ).pack(side="left", padx=10)
        ttk.Button(ctrl_row, text="Clear", command=self._clear_graph).pack(side="right", padx=2)

        cal_row = ttk.Frame(color_frame)
        cal_row.pack(fill="x", pady=(6, 0))

        ttk.Button(cal_row, text="Calibrate White", command=self._calibrate_white).pack(side="left", padx=2)
        ttk.Button(cal_row, text="Reset Cal", command=self._reset_calibration).pack(side="left", padx=2)

        self.cal_status_label = ttk.Label(color_frame, text="Not calibrated", foreground="#f39c12",
                                           font=("TkDefaultFont", 9, "bold"))
        self.cal_status_label.pack(pady=(4, 0))

        if HAS_MATPLOTLIB:
            self.fig = Figure(figsize=(4, 2), dpi=80)
            self.fig.patch.set_facecolor("#f0f0f0")
            self.ax = self.fig.add_subplot(111)
            self.ax.set_xlabel("Time (s)", fontsize=8)
            self.ax.set_ylabel("Count", fontsize=8)
            self.ax.tick_params(labelsize=7)
            self.ax.grid(True, alpha=0.3)
            self.lines = [
                self.ax.plot([], [], "-o", markersize=2, linewidth=1,
                             color=COLOR_AXIS_COLORS[i], label=COLOR_AXIS_NAMES[i])[0]
                for i in range(3)
            ]
            self.ax.legend(fontsize=7, loc="upper left")
            self.fig.tight_layout()

            self.canvas = FigureCanvasTkAgg(self.fig, master=self)
            self.canvas.get_tk_widget().pack(fill="both", expand=True, pady=(4, 0))

    def _test_connection(self):
        def worker():
            resp = send_command(self.ip, "COLOR_READ")
            self.after(0, lambda: self._on_test_result(resp))
        threading.Thread(target=worker, daemon=True).start()

    def _on_test_result(self, resp):
        if resp is not None and resp.startswith("R:"):
            self.status_label.config(text="Reachable", foreground="green")
            self.log(self.board_name, f"Board reachable - {resp}")
            self._apply_reading(resp, record=False)
        else:
            self.status_label.config(text="Unreachable", foreground="red")
            self.log(self.board_name, f"Cannot reach {self.ip}")

    def _read_color(self):
        def worker():
            self.after(0, lambda: self.log(self.board_name, ">> COLOR_READ"))
            resp = send_command(self.ip, "COLOR_READ")
            if resp is None:
                self.after(0, self._on_error)
                return
            self.after(0, lambda: self._on_color_response(resp))
        threading.Thread(target=worker, daemon=True).start()

    def _on_color_response(self, resp):
        self.log(self.board_name, f"<< {resp}")
        if resp.startswith("R:"):
            self.status_label.config(text="Reachable", foreground="green")
            self._apply_reading(resp, record=True)
        else:
            self.status_label.config(text="Unreachable", foreground="red")

    def _poll_loop(self):
        """Background thread: polls COLOR_READ at 4 Hz, forever, regardless
        of the 'Live' checkbox, so cube detection is always active."""
        while self._poll_running:
            resp = send_command(self.ip, "COLOR_READ")
            self.after(0, lambda r=resp: self._on_poll_result(r))
            time.sleep(COLOR_AUTO_REFRESH_MS / 1000.0)

    def _on_poll_result(self, resp):
        if resp is None or not resp.startswith("R:"):
            if self.auto_refresh_var.get():
                self.status_label.config(text="Unreachable", foreground="red")
            return

        self.status_label.config(text="Reachable", foreground="green")

        parsed = self._parse_reading(resp)
        if parsed is not None:
            self._check_cube_trigger(*parsed)

        if self.auto_refresh_var.get():
            self.log(self.board_name, f"<< {resp}")
            self._apply_reading(resp, record=True)

    def _check_cube_trigger(self, r, g, b):
        """Detect a color cube under the sensor from raw RGB counts and
        trigger the matching servo sequence. Runs on every poll."""
        max_val = max(r, g, b)

        if max_val < COLOR_CUBE_THRESHOLD:
            self._cube_color = None
            self._cube_handled = False
            self._cube_ignore_logged = False
            return

        if max_val == r:
            color = "RED"
        elif max_val == g:
            color = "GREEN"
        else:
            color = "BLUE"

        if color != self._cube_color:
            self._cube_color = color
            self._cube_handled = False
            self._cube_ignore_logged = False
            self.log(self.board_name, f"Cube detected: {color} (R={r} G={g} B={b})")

        if self._cube_handled:
            return

        if self._sequence_active:
            if not self._cube_ignore_logged:
                self.log(self.board_name, "Sequence already active - ignoring new detection")
                self._cube_ignore_logged = True
            return

        self._cube_handled = True

        if color == "RED":
            self._start_red_sequence()
        elif color == "GREEN":
            self._start_green_sequence()
        else:
            self.log(self.board_name, "Blue cube - no servo action")

    def _send_servo_command(self, cmd):
        if self.servo_panel is None:
            self.log(self.board_name, f"No servo panel wired - cannot send {cmd}")
            return
        self.servo_panel._send(cmd)

    def _start_red_sequence(self):
        self._sequence_active = True
        self.log(self.board_name,
                 f"RED cube -> SERVO1 ON in {RED_SERVO_DELAY_S}s, "
                 f"for {RED_SERVO_ON_DURATION_S}s")
        self.after(int(RED_SERVO_DELAY_S * 1000), self._red_servo_on)

    def _red_servo_on(self):
        self._send_servo_command("SERVO1_ON")
        self.after(int(RED_SERVO_ON_DURATION_S * 1000), self._red_servo_off)

    def _red_servo_off(self):
        self._send_servo_command("SERVO1_OFF")
        self._sequence_active = False

    def _start_green_sequence(self):
        self._sequence_active = True
        self.log(self.board_name,
                 f"GREEN cube -> SERVO2 ON in {GREEN_SERVO_DELAY_S}s, "
                 f"for {GREEN_SERVO_ON_DURATION_S}s")
        self.after(int(GREEN_SERVO_DELAY_S * 1000), self._green_servo_on)

    def _green_servo_on(self):
        self._send_servo_command("SERVO2_ON")
        self.after(int(GREEN_SERVO_ON_DURATION_S * 1000), self._green_servo_off)

    def _green_servo_off(self):
        self._send_servo_command("SERVO2_OFF")
        self._sequence_active = False

    def _parse_reading(self, resp):
        vals = {}
        for tok in resp.replace(",", " ").split():
            if ":" in tok:
                key, _, val = tok.partition(":")
                try:
                    vals[key] = int(val)
                except ValueError:
                    continue
        if "R" not in vals or "G" not in vals or "B" not in vals:
            return None
        return vals["R"], vals["G"], vals["B"]

    def _apply_reading(self, resp, record):
        parsed = self._parse_reading(resp)
        if parsed is None:
            return
        r, g, b = parsed

        if self.cal_gains:
            gr, gg, gb = self.cal_gains
            r, g, b = (
                min(int(r * gr), 65535),
                min(int(g * gg), 65535),
                min(int(b * gb), 65535),
            )

        hexcol = rgb_counts_to_hex(r, g, b)
        self.swatch.itemconfig(self.swatch_rect, fill=hexcol)
        self.rgb_label.config(text=f"R: {r}   G: {g}   B: {b}")
        self.hex_label.config(text=hexcol)

        if record:
            if self.start_time is None:
                self.start_time = datetime.now()
            elapsed = (datetime.now() - self.start_time).total_seconds()
            self.time_history.append(elapsed)
            for hist, val in zip(self.rgb_history, (r, g, b)):
                hist.append(val)
            self._update_graph()

    def _on_error(self):
        self.log(self.board_name, "<< ERROR: no response")
        self.status_label.config(text="Unreachable", foreground="red")

    def _calibrate_white(self):
        def worker():
            resp = send_command(self.ip, "COLOR_READ")
            self.after(0, lambda: self._on_calibrate_response(resp))
        threading.Thread(target=worker, daemon=True).start()

    def _on_calibrate_response(self, resp):
        parsed = self._parse_reading(resp) if resp else None
        if parsed is None:
            self.log(self.board_name, "Calibration failed - no reading")
            return
        ref_r, ref_g, ref_b = parsed

        if max(ref_r, ref_g, ref_b) < 10:
            self.log(self.board_name,
                     "Reading too low to calibrate - check sensor placement/lighting")
            return

        target = max(ref_r, ref_g, ref_b)
        self.cal_gains = (
            target / max(ref_r, 1),
            target / max(ref_g, 1),
            target / max(ref_b, 1),
        )
        self.cal_status_label.config(text="Calibrated", foreground="#2ecc71")
        self.log(self.board_name,
                 f"White calibration set from R={ref_r} G={ref_g} B={ref_b} "
                 f"(gains: {self.cal_gains[0]:.2f}, {self.cal_gains[1]:.2f}, "
                 f"{self.cal_gains[2]:.2f})")

    def _reset_calibration(self):
        self.cal_gains = None
        self.cal_status_label.config(text="Not calibrated", foreground="#f39c12")
        self.log(self.board_name, "Calibration reset")

    def _update_graph(self):
        if not HAS_MATPLOTLIB or not self.time_history:
            return
        times = list(self.time_history)
        all_vals = []
        for line, hist in zip(self.lines, self.rgb_history):
            vals = list(hist)
            line.set_data(times, vals)
            all_vals.extend(vals)
        self.ax.set_xlim(max(0, times[0] - 2), times[-1] + 2)
        if all_vals:
            v_min, v_max = min(all_vals), max(all_vals)
            margin = max(50, (v_max - v_min) * 0.2)
            self.ax.set_ylim(max(0, v_min - margin), v_max + margin)
        self.canvas.draw_idle()

    def _clear_graph(self):
        for hist in self.rgb_history:
            hist.clear()
        self.time_history.clear()
        self.start_time = None
        if HAS_MATPLOTLIB:
            for line in self.lines:
                line.set_data([], [])
            self.ax.set_xlim(0, 10)
            self.ax.set_ylim(0, 100)
            self.canvas.draw_idle()

    def _toggle_auto_refresh(self):
        # Background polling (_poll_loop) always runs; this only controls
        # whether polled readings are reflected in the swatch/labels/graph.
        pass

    def cleanup(self):
        self.auto_refresh_var.set(False)
        self._poll_running = False


class ServoBoardPanel(ttk.LabelFrame):
    """UI panel for an APARD board driving two servomotors."""

    def __init__(self, parent, board_name, ip, log_callback):
        super().__init__(parent, text=f"  {board_name} ({ip})  ", padding=10)
        self.board_name = board_name
        self.ip = ip
        self.log = log_callback
        self._build_ui()

    def _build_ui(self):
        status_frame = ttk.Frame(self)
        status_frame.pack(fill="x", pady=(0, 8))

        ttk.Label(status_frame, text="Status:").pack(side="left")
        self.status_label = ttk.Label(
            status_frame, text="Unknown", foreground="gray",
            font=("TkDefaultFont", 10, "bold")
        )
        self.status_label.pack(side="left", padx=6)
        ttk.Button(status_frame, text="Test", command=self._test_connection).pack(side="right")

        servo_frame = ttk.LabelFrame(self, text="Servo Control", padding=6)
        servo_frame.pack(fill="x", pady=4)

        s1_row = ttk.Frame(servo_frame)
        s1_row.pack(fill="x", pady=2)
        ttk.Label(s1_row, text="Servo 1:", width=8).pack(side="left")
        ttk.Button(s1_row, text="ON", command=lambda: self._send("SERVO1_ON")).pack(
            side="left", padx=2, expand=True, fill="x")
        ttk.Button(s1_row, text="OFF", command=lambda: self._send("SERVO1_OFF")).pack(
            side="left", padx=2, expand=True, fill="x")

        s2_row = ttk.Frame(servo_frame)
        s2_row.pack(fill="x", pady=2)
        ttk.Label(s2_row, text="Servo 2:", width=8).pack(side="left")
        ttk.Button(s2_row, text="ON", command=lambda: self._send("SERVO2_ON")).pack(
            side="left", padx=2, expand=True, fill="x")
        ttk.Button(s2_row, text="OFF", command=lambda: self._send("SERVO2_OFF")).pack(
            side="left", padx=2, expand=True, fill="x")

        status_row = ttk.Frame(servo_frame)
        status_row.pack(fill="x", pady=(4, 0))
        ttk.Button(status_row, text="Servo Status", command=lambda: self._send("SERVO_STATUS")).pack(
            fill="x", padx=2)

        self.servo1_state_label = ttk.Label(servo_frame, text="Servo 1: --", font=("TkDefaultFont", 10))
        self.servo1_state_label.pack(pady=(6, 0))
        self.servo2_state_label = ttk.Label(servo_frame, text="Servo 2: --", font=("TkDefaultFont", 10))
        self.servo2_state_label.pack(pady=(2, 0))

    def _test_connection(self):
        def worker():
            resp = send_command(self.ip, "SERVO_STATUS")
            self.after(0, lambda: self._on_test_result(resp))
        threading.Thread(target=worker, daemon=True).start()

    def _on_test_result(self, resp):
        if resp is not None:
            self.status_label.config(text="Reachable", foreground="green")
            self.log(self.board_name, f"Board reachable at {self.ip}")
            self._parse_status(resp)
        else:
            self.status_label.config(text="Unreachable", foreground="red")
            self.log(self.board_name, f"Cannot reach {self.ip}")

    def _send(self, cmd):
        def worker():
            self.after(0, lambda: self.log(self.board_name, f">> {cmd}"))
            resp = send_command(self.ip, cmd)
            if resp is None:
                self.after(0, lambda: self._on_error(cmd))
                return
            self.after(0, lambda: self._on_response(cmd, resp))
        threading.Thread(target=worker, daemon=True).start()

    def _on_response(self, cmd, resp):
        self.log(self.board_name, f"<< {resp}")
        self.status_label.config(text="Reachable", foreground="green")
        if cmd == "SERVO1_ON" and resp == "OK":
            self.servo1_state_label.config(text="Servo 1: ON")
        elif cmd == "SERVO1_OFF" and resp == "OK":
            self.servo1_state_label.config(text="Servo 1: OFF")
        elif cmd == "SERVO2_ON" and resp == "OK":
            self.servo2_state_label.config(text="Servo 2: ON")
        elif cmd == "SERVO2_OFF" and resp == "OK":
            self.servo2_state_label.config(text="Servo 2: OFF")
        elif cmd == "SERVO_STATUS":
            self._parse_status(resp)

    def _parse_status(self, resp):
        tokens = resp.replace(",", " ").split()
        for tok in tokens:
            if tok.startswith("SERVO1:"):
                self.servo1_state_label.config(text=f"Servo 1: {tok[7:]}")
            elif tok.startswith("SERVO2:"):
                self.servo2_state_label.config(text=f"Servo 2: {tok[7:]}")

    def _on_error(self, cmd):
        self.log(self.board_name, f"<< ERROR: no response to {cmd}")
        self.status_label.config(text="Unreachable", foreground="red")


class CN0575Panel(ttk.LabelFrame):
    """UI panel for CN0575 with ADT75 temperature graph."""

    def __init__(self, parent, log_callback):
        super().__init__(parent, text=f"  CN0575 — ADT75 Sensor ({CN0575_IP})  ", padding=10)
        self.log = log_callback
        self.ip = CN0575_IP
        self.board_name = "CN0575"
        self.temp_history = deque(maxlen=GRAPH_MAX_POINTS)
        self.time_history = deque(maxlen=GRAPH_MAX_POINTS)
        self.auto_refresh_var = tk.BooleanVar(value=False)
        self.auto_refresh_job = None
        self.start_time = None
        self._build_ui()

    def _build_ui(self):
        status_frame = ttk.Frame(self)
        status_frame.pack(fill="x", pady=(0, 8))

        ttk.Label(status_frame, text="Status:").pack(side="left")
        self.status_label = ttk.Label(
            status_frame, text="Unknown", foreground="gray",
            font=("TkDefaultFont", 10, "bold")
        )
        self.status_label.pack(side="left", padx=6)
        ttk.Button(status_frame, text="Test", command=self._test_connection).pack(side="right")

        temp_value_frame = ttk.Frame(self)
        temp_value_frame.pack(fill="x", pady=4)

        self.temp_label = ttk.Label(
            temp_value_frame, text="ADT75 Temp: -- C",
            font=("TkDefaultFont", 14, "bold")
        )
        self.temp_label.pack(side="left", padx=5)

        ctrl_frame = ttk.Frame(self)
        ctrl_frame.pack(fill="x", pady=4)

        ttk.Button(ctrl_frame, text="Read Temp", command=self._read_temp).pack(side="left", padx=2)
        ttk.Checkbutton(
            ctrl_frame, text="Live (5s)",
            variable=self.auto_refresh_var,
            command=self._toggle_auto_refresh
        ).pack(side="left", padx=10)
        ttk.Button(ctrl_frame, text="Clear", command=self._clear_graph).pack(side="right", padx=2)

        if HAS_MATPLOTLIB:
            self.fig = Figure(figsize=(4, 2), dpi=80)
            self.fig.patch.set_facecolor("#f0f0f0")
            self.ax = self.fig.add_subplot(111)
            self.ax.set_xlabel("Time (s)", fontsize=8)
            self.ax.set_ylabel("Temp (C)", fontsize=8)
            self.ax.tick_params(labelsize=7)
            self.ax.grid(True, alpha=0.3)
            self.line, = self.ax.plot([], [], "b-o", markersize=2, linewidth=1)
            self.fig.tight_layout()

            self.canvas = FigureCanvasTkAgg(self.fig, master=self)
            self.canvas.get_tk_widget().pack(fill="both", expand=True, pady=(4, 0))

    def _test_connection(self):
        def worker():
            resp = send_command(self.ip, "READ_TEMP")
            self.after(0, lambda: self._on_test_result(resp))
        threading.Thread(target=worker, daemon=True).start()

    def _on_test_result(self, resp):
        if resp is not None and resp.startswith("TEMP:"):
            self.status_label.config(text="Reachable", foreground="green")
            self.log(self.board_name, f"Sensor reachable - {resp}")
            self.temp_label.config(text=f"ADT75 Temp: {resp[5:]} C")
        else:
            self.status_label.config(text="Unreachable", foreground="red")
            self.log(self.board_name, f"Cannot reach {self.ip}")

    def _read_temp(self):
        def worker():
            self.after(0, lambda: self.log(self.board_name, ">> READ_TEMP"))
            resp = send_command(self.ip, "READ_TEMP")
            if resp is None:
                self.after(0, self._on_error)
                return
            self.after(0, lambda: self._on_temp_response(resp))
        threading.Thread(target=worker, daemon=True).start()

    def _on_temp_response(self, resp):
        self.log(self.board_name, f"<< {resp}")
        self.status_label.config(text="Reachable", foreground="green")

        if resp.startswith("TEMP:"):
            try:
                temp = float(resp[5:])
            except ValueError:
                return
            self.temp_label.config(text=f"ADT75 Temp: {temp:.1f} C")
            if self.start_time is None:
                self.start_time = datetime.now()
            elapsed = (datetime.now() - self.start_time).total_seconds()
            self.time_history.append(elapsed)
            self.temp_history.append(temp)
            self._update_graph()

    def _on_error(self):
        self.log(self.board_name, "<< ERROR: no response")
        self.status_label.config(text="Unreachable", foreground="red")

    def _update_graph(self):
        if not HAS_MATPLOTLIB or not self.temp_history:
            return
        times = list(self.time_history)
        temps = list(self.temp_history)
        self.line.set_data(times, temps)
        self.ax.set_xlim(max(0, times[0] - 2), times[-1] + 2)
        if len(temps) > 1:
            t_min, t_max = min(temps), max(temps)
            margin = max(0.5, (t_max - t_min) * 0.2)
            self.ax.set_ylim(t_min - margin, t_max + margin)
        else:
            self.ax.set_ylim(temps[0] - 2, temps[0] + 2)
        self.canvas.draw_idle()

    def _clear_graph(self):
        self.temp_history.clear()
        self.time_history.clear()
        self.start_time = None
        if HAS_MATPLOTLIB:
            self.line.set_data([], [])
            self.ax.set_xlim(0, 10)
            self.ax.set_ylim(20, 30)
            self.canvas.draw_idle()

    def _toggle_auto_refresh(self):
        if self.auto_refresh_var.get():
            self._auto_refresh_tick()
        else:
            if self.auto_refresh_job:
                self.after_cancel(self.auto_refresh_job)
                self.auto_refresh_job = None

    def _auto_refresh_tick(self):
        if self.auto_refresh_var.get():
            self._read_temp()
            self.auto_refresh_job = self.after(AUTO_REFRESH_MS, self._auto_refresh_tick)

    def cleanup(self):
        self.auto_refresh_var.set(False)
        if self.auto_refresh_job:
            self.after_cancel(self.auto_refresh_job)


class SWIOT1LPanel(ttk.LabelFrame):
    """UI panel for SWIOT1L fan PWM control."""

    def __init__(self, parent, log_callback):
        super().__init__(parent, text=f"  SWIOT1L — Fan Control ({SWIOT_IP})  ", padding=10)
        self.log = log_callback
        self.board_name = "SWIOT1L"
        self.connected = False
        self.max14906 = None
        self.duty_cycle = 0.0
        self.pwm_running = False
        self.speed_history = deque(maxlen=GRAPH_MAX_POINTS)
        self.time_history = deque(maxlen=GRAPH_MAX_POINTS)
        self.start_time = None
        self._build_ui()

    def _build_ui(self):
        status_frame = ttk.Frame(self)
        status_frame.pack(fill="x", pady=(0, 8))

        ttk.Label(status_frame, text="Status:").pack(side="left")
        self.status_label = ttk.Label(
            status_frame, text="Disconnected", foreground="gray",
            font=("TkDefaultFont", 10, "bold")
        )
        self.status_label.pack(side="left", padx=6)
        ttk.Button(status_frame, text="Connect", command=self._connect).pack(side="right")

        ctrl_frame = ttk.LabelFrame(self, text="PWM Control", padding=6)
        ctrl_frame.pack(fill="x", pady=4)

        dc_row = ttk.Frame(ctrl_frame)
        dc_row.pack(fill="x")

        ttk.Label(dc_row, text="Duty (%):").pack(side="left")
        self.dc_entry = ttk.Entry(dc_row, width=6)
        self.dc_entry.insert(0, "0")
        self.dc_entry.pack(side="left", padx=4)

        ttk.Button(dc_row, text="Set", command=self._set_pwm).pack(side="left", padx=2)
        ttk.Button(dc_row, text="Stop", command=self._stop_pwm).pack(side="left", padx=2)

        self.dc_label = ttk.Label(ctrl_frame, text="0% - 0 RPM", font=("TkDefaultFont", 10))
        self.dc_label.pack(pady=(6, 0))

        if HAS_MATPLOTLIB:
            self.fig = Figure(figsize=(4, 2), dpi=80)
            self.fig.patch.set_facecolor("#f0f0f0")
            self.ax = self.fig.add_subplot(111)
            self.ax.set_xlabel("Time (s)", fontsize=8)
            self.ax.set_ylabel("RPM", fontsize=8)
            self.ax.tick_params(labelsize=7)
            self.ax.grid(True, alpha=0.3)
            self.line, = self.ax.plot([], [], "r-o", markersize=2, linewidth=1)
            self.fig.tight_layout()

            self.canvas = FigureCanvasTkAgg(self.fig, master=self)
            self.canvas.get_tk_widget().pack(fill="both", expand=True, pady=(4, 0))

    def _connect(self):
        if not HAS_ADI:
            self.log(self.board_name, "pyadi-iio not installed")
            return

        self.status_label.config(text="Connecting...", foreground="orange")
        self.log(self.board_name, "Connecting to SWIOT1L...")

        def worker():
            try:
                swiot = adi.swiot(uri=f"ip:{SWIOT_IP}")
                swiot.mode = "config"
                swiot = adi.swiot(uri=f"ip:{SWIOT_IP}")
                swiot.ch0_device = "max14906"
                swiot.ch0_function = "output"
                swiot.ch0_enable = 1
                swiot.ch1_device = "ad74413r"
                swiot.ch1_function = "voltage_in"
                swiot.ch1_enable = 1
                swiot.ch2_device = "ad74413r"
                swiot.ch2_function = "voltage_in"
                swiot.ch2_enable = 1
                swiot.ch3_device = "ad74413r"
                swiot.ch3_function = "high_z"
                swiot.ch3_enable = 1
                swiot.mode = "runtime"
                self.max14906 = adi.max14906(uri=f"ip:{SWIOT_IP}")
                swiot = adi.swiot(uri=f"ip:{SWIOT_IP}")
                swiot.mode = "runtime"
                self.connected = True
                self.after(0, self._on_connected)
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                self.after(0, lambda: self._on_connect_error(msg))

        threading.Thread(target=worker, daemon=True).start()

    def _on_connected(self):
        self.status_label.config(text="Connected", foreground="green")
        self.log(self.board_name, "Connected - ready for PWM")

    def _on_connect_error(self, err):
        self.status_label.config(text="Error", foreground="red")
        self.log(self.board_name, f"Connection failed: {err}")

    def _set_pwm(self):
        if not self.connected:
            self.log(self.board_name, "Not connected")
            return
        try:
            dc = float(self.dc_entry.get())
            dc = max(0.0, min(100.0, dc))
        except ValueError:
            self.log(self.board_name, "Invalid duty cycle")
            return

        self.duty_cycle = dc
        rpm = DC_RPM * dc / 100.0
        self.dc_label.config(text=f"{dc:.0f}% - {rpm:.0f} RPM")
        self.log(self.board_name, f"Duty cycle set to {dc:.0f}%")

        if not self.pwm_running:
            self.pwm_running = True
            threading.Thread(target=self._pwm_loop, daemon=True).start()
            self._graph_tick()

    def _stop_pwm(self):
        self.pwm_running = False
        self.duty_cycle = 0.0
        self.dc_label.config(text="0% - 0 RPM")
        if self.connected and self.max14906:
            try:
                self.max14906.channel["voltage0"].raw = 0
            except Exception:
                pass
        self.log(self.board_name, "PWM stopped")

    def _pwm_loop(self):
        while self.pwm_running and self.connected:
            dc = self.duty_cycle
            try:
                if dc <= 0:
                    self.max14906.channel["voltage0"].raw = 0
                    time.sleep(PWM_PERIOD)
                elif dc >= 100:
                    self.max14906.channel["voltage0"].raw = 1
                    time.sleep(PWM_PERIOD)
                else:
                    on_time = PWM_PERIOD * dc / 100.0
                    off_time = PWM_PERIOD - on_time
                    self.max14906.channel["voltage0"].raw = 1
                    time.sleep(on_time)
                    self.max14906.channel["voltage0"].raw = 0
                    time.sleep(off_time)
            except Exception:
                break

    def _graph_tick(self):
        if not self.pwm_running:
            return
        if self.start_time is None:
            self.start_time = datetime.now()
        elapsed = (datetime.now() - self.start_time).total_seconds()
        rpm = DC_RPM * self.duty_cycle / 100.0
        self.time_history.append(elapsed)
        self.speed_history.append(rpm)
        self._update_graph()
        if self.pwm_running:
            self.after(1000, self._graph_tick)

    def _update_graph(self):
        if not HAS_MATPLOTLIB or not self.speed_history:
            return
        times = list(self.time_history)
        speeds = list(self.speed_history)
        self.line.set_data(times, speeds)
        self.ax.set_xlim(max(0, times[0] - 2), times[-1] + 2)
        if len(speeds) > 1:
            s_min, s_max = min(speeds), max(speeds)
            margin = max(100, (s_max - s_min) * 0.2)
            self.ax.set_ylim(max(0, s_min - margin), s_max + margin)
        else:
            self.ax.set_ylim(0, DC_RPM + 500)
        self.canvas.draw_idle()

    def cleanup(self):
        self.pwm_running = False
        if self.connected and self.max14906:
            try:
                self.max14906.channel["voltage0"].raw = 0
            except Exception:
                pass


class ADXL355Panel(ttk.LabelFrame):
    """Compact ADXL355 predictive maintenance panel."""

    def __init__(self, parent, log_callback, use_demo=False):
        host_info = "Demo" if use_demo else f"{args.adxl_host}:{args.adxl_port}"
        super().__init__(parent, text=f"  ADXL355 — Vibration Monitor ({host_info})  ", padding=10)
        self.log = log_callback
        self.board_name = "ADXL355"
        self.use_demo = use_demo
        self.acq = None
        self._running = False
        self._update_id = None
        self._prev_level = ["OK"] * 3

        # FFT helpers
        self._window = np.hanning(ADXL_FFT_WIN)
        self._win_norm = self._window.sum()
        freqs = np.fft.rfftfreq(ADXL_FFT_WIN, d=1.0 / ADXL_FS)
        self._fmask = freqs <= ADXL_FREQ_MAX
        self._fdisp = freqs[self._fmask]

        self._build_ui()

    def _build_ui(self):
        # Status row
        status_frame = ttk.Frame(self)
        status_frame.pack(fill="x", pady=(0, 8))

        ttk.Label(status_frame, text="Status:").pack(side="left")
        self.status_label = ttk.Label(
            status_frame, text="Stopped", foreground="gray",
            font=("TkDefaultFont", 10, "bold")
        )
        self.status_label.pack(side="left", padx=6)

        self._btn_stop = ttk.Button(status_frame, text="Stop", command=self._stop, state=tk.DISABLED)
        self._btn_stop.pack(side="right", padx=2)
        self._btn_start = ttk.Button(status_frame, text="Start", command=self._start)
        self._btn_start.pack(side="right", padx=2)

        # Axis status indicators
        axis_frame = ttk.LabelFrame(self, text="Axis Health", padding=4)
        axis_frame.pack(fill="x", pady=4)

        self._status_canvases = []
        self._status_labels = []
        self._rms_labels = []

        for i, name in enumerate(AXIS_NAMES):
            row = ttk.Frame(axis_frame)
            row.pack(fill="x", padx=4, pady=1)

            c = tk.Canvas(row, width=12, height=12, highlightthickness=0)
            c.pack(side="left", padx=(0, 4))
            oval = c.create_oval(1, 1, 11, 11, fill="#2ecc71", outline="")
            self._status_canvases.append((c, oval))

            ttk.Label(row, text=f"{name}:", width=3).pack(side="left")
            sl = ttk.Label(row, text="OK", width=8, foreground="#2ecc71",
                           font=("TkDefaultFont", 9, "bold"))
            sl.pack(side="left")
            self._status_labels.append(sl)

            rl = ttk.Label(row, text="RMS: --", font=("Courier", 8))
            rl.pack(side="right")
            self._rms_labels.append(rl)

        # FFT plot (single axis selector + plot)
        if HAS_MATPLOTLIB:
            plot_ctrl = ttk.Frame(self)
            plot_ctrl.pack(fill="x", pady=2)
            ttk.Label(plot_ctrl, text="Show axis:").pack(side="left")
            self._axis_var = tk.IntVar(value=0)
            for i, name in enumerate(AXIS_NAMES):
                ttk.Radiobutton(plot_ctrl, text=name, variable=self._axis_var, value=i).pack(side="left", padx=2)

            self.fig = Figure(figsize=(4, 2.2), dpi=80)
            self.fig.patch.set_facecolor("#f0f0f0")
            self.ax = self.fig.add_subplot(111)
            self.ax.set_xlabel("Hz", fontsize=8)
            self.ax.set_ylabel("g", fontsize=8)
            self.ax.set_title("FFT", fontsize=9)
            self.ax.tick_params(labelsize=7)
            self.ax.grid(True, alpha=0.3)
            self.ax.set_xlim(0, ADXL_FREQ_MAX)
            self.line, = self.ax.plot([], [], lw=1, color=AXIS_COLORS[0])
            self.fig.tight_layout()

            self.canvas = FigureCanvasTkAgg(self.fig, master=self)
            self.canvas.get_tk_widget().pack(fill="both", expand=True, pady=(4, 0))

    def _start(self):
        if self._running:
            return

        try:
            if self.use_demo:
                source = DemoSource()
            else:
                source = TCPSource(args.adxl_host, args.adxl_port)
                global ADXL_FS, ADXL_CHUNK, ADXL_FREQ_MAX
                ADXL_FS = source.fs
                ADXL_CHUNK = source.chunk
                ADXL_FREQ_MAX = min(500, ADXL_FS / 2)

            self.acq = ADXL355Acquisition(source)
            self.acq.start()
            self._running = True
            self.status_label.config(text="Running", foreground="green")
            self._btn_start.config(state=tk.DISABLED)
            self._btn_stop.config(state=tk.NORMAL)
            self.log(self.board_name, f"Started - FS={ADXL_FS}Hz")
            self._schedule_update()
        except Exception as e:
            self.status_label.config(text="Error", foreground="red")
            self.log(self.board_name, f"Connection failed: {e}")

    def _stop(self):
        self._running = False
        if self.acq:
            self.acq.stop()
        if self._update_id:
            self.after_cancel(self._update_id)
            self._update_id = None
        self.status_label.config(text="Stopped", foreground="gray")
        self._btn_start.config(state=tk.NORMAL)
        self._btn_stop.config(state=tk.DISABLED)
        self.log(self.board_name, "Stopped")

    def _schedule_update(self):
        if self._running:
            self._update()
            self._update_id = self.after(100, self._schedule_update)

    def _update(self):
        if not self.acq:
            return
        bufs = self.acq.snapshot()

        for i, samples in enumerate(bufs):
            rms, peak, cf, kurt = compute_metrics(samples)
            level = health_level(rms, cf, kurt)

            c, oval = self._status_canvases[i]
            col = LEVEL_COLOR[level]
            c.itemconfig(oval, fill=col)
            self._status_labels[i].config(text=level, foreground=col)
            self._rms_labels[i].config(text=f"RMS:{rms:.3f}g CF:{cf:.1f}")

            if level != self._prev_level[i]:
                self.log(self.board_name,
                         f"{AXIS_NAMES[i]}: {self._prev_level[i]} -> {level} (RMS={rms:.3f}g)")
                self._prev_level[i] = level

        # Update FFT plot for selected axis
        if HAS_MATPLOTLIB:
            axis_idx = self._axis_var.get()
            samples = bufs[axis_idx]
            sig = (samples - samples.mean()) * self._window
            fft_mag = np.abs(np.fft.rfft(sig)) / self._win_norm
            mag = fft_mag[self._fmask]

            self.line.set_data(self._fdisp, mag)
            self.line.set_color(AXIS_COLORS[axis_idx])
            pad = (mag.max() - mag.min()) * 0.15 + 1e-6
            self.ax.set_ylim(mag.min() - pad, mag.max() + pad)
            self.ax.set_title(f"FFT - {AXIS_NAMES[axis_idx]}", fontsize=9)
            self.canvas.draw_idle()

    def cleanup(self):
        self._stop()


# ---------------------------------------------------------------------------
# Main Application Window
# ---------------------------------------------------------------------------
class ControlPanel(tk.Tk):
    """Main application window with all panels."""

    def __init__(self):
        super().__init__()
        self.title("Industrial Demo — Control Panel")
        self.geometry("1400x950")
        self.minsize(1200, 800)

        # Top row: APARD boards
        top_frame = ttk.Frame(self)
        top_frame.pack(fill="x", padx=10, pady=5)

        self.board1 = ServoBoardPanel(top_frame, "APARD #1", APARD1_IP, self._log_message)
        self.board1.pack(side="left", fill="both", expand=True, padx=(0, 5))

        self.board2 = ColorSensorPanel(top_frame, "APARD #2", APARD2_IP, self._log_message,
                                        servo_panel=self.board1)
        self.board2.pack(side="left", fill="both", expand=True, padx=(5, 0))

        # Middle row: CN0575 + SWIOT1L + ADXL355
        middle_frame = ttk.Frame(self)
        middle_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.cn0575 = CN0575Panel(middle_frame, self._log_message)
        self.cn0575.pack(side="left", fill="both", expand=True, padx=(0, 5))

        self.swiot = SWIOT1LPanel(middle_frame, self._log_message)
        self.swiot.pack(side="left", fill="both", expand=True, padx=5)

        self.adxl355 = ADXL355Panel(middle_frame, self._log_message, use_demo=args.demo)
        self.adxl355.pack(side="left", fill="both", expand=True, padx=(5, 0))

        # Log area
        log_frame = ttk.LabelFrame(self, text="Communication Log", padding=5)
        log_frame.pack(fill="both", padx=10, pady=5)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=6, state="disabled", font=("Courier", 9)
        )
        self.log_text.pack(fill="both", expand=True)

        # Bottom buttons
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=(0, 10))

        ttk.Button(btn_frame, text="Test All", command=self._test_all).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Clear Log", command=self._clear_log).pack(side="left", padx=5)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _log_message(self, board_name, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        entry = f"[{timestamp}] {board_name}: {message}\n"
        self.log_text.config(state="normal")
        self.log_text.insert("end", entry)
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _test_all(self):
        self.board1._test_connection()
        self.board2._test_connection()
        self.cn0575._test_connection()

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def _on_close(self):
        self.board2.cleanup()
        self.cn0575.cleanup()
        self.swiot.cleanup()
        self.adxl355.cleanup()
        self.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app = ControlPanel()
    app.mainloop()

