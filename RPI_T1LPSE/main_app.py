#!/usr/bin/env python3
"""
ADI DataX™ - AI-driven 10BASE-T1L Deployment
(color sensor variant: COLOR_READ polled at 4 Hz over IP)

Combined application integrating:
- APARD board control (Servo, TCS34725 color sensor)
- CN0575 temperature monitoring
- SWIOT1L fan PWM control
- ADXL355 predictive maintenance monitor

Styled to the design system in CLAUDE.md (see design_system.py): approved
palette, light/dark themes, card-based panels, 8px-radius primary buttons and
the 4-48px spacing scale.

Requirements:
    pip3 install matplotlib pyadi-iio numpy

Usage:
    python3 main_app.py                     # real hardware, light theme
    python3 main_app.py --theme dark        # real hardware, dark theme
    python3 main_app.py --demo              # synthetic data for every panel
    python3 main_app.py --adxl-host IP      # ADXL355 server address
"""

import argparse
import json
import math
import os
import socket
import struct
import subprocess
import threading
import time
from datetime import datetime
from collections import deque
import numpy as np
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, scrolledtext

from design_system import (
    PRIMARY, NEUTRAL, SUCCESS, WARNING, ERROR, INFO,
    THEMES, XS, SM, MD, LG, XL, XXL,
    STATUS_COLOR, LEVEL_COLOR, AXIS_COLORS, COLOR_AXIS_COLORS,
    pick_font, Button, Card, ThemeMixin,
)

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
parser.add_argument("--demo", action="store_true",
                    help="Synthetic data for every panel (no hardware needed)")
parser.add_argument("--theme", choices=["light", "dark"], default="light")
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

# Remote server launch (the "Start Servers" button on the ADXL355 panel).
# Both scripts are accept-loop servers that never return, so they must be
# started detached and concurrently -- running them in sequence would block on
# the first and never bind the ADXL port.
REMOTE_HOST = f"analog@{CN0575_IP}"
REMOTE_DIR = "industrial-demo/RPI_CN0575"
REMOTE_LOG_DIR = "/tmp"
SSH_CONNECT_TIMEOUT = 8          # seconds; the ssh call itself returns at once
REMOTE_SERVERS = (
    # (label, script, args, log basename)
    ("CN0575 command server", "cn0575_state_machine.py", "", "cn0575_state_machine"),
    ("ADXL355 data server", "adxl355_server.py",
     f"--rate {ADXL_FS} --chunk {ADXL_CHUNK} --port {args.adxl_port}",
     "adxl355_server"),
)

# Predictive maintenance thresholds
THRESH_WARN = 0.10
THRESH_ALARM = 0.50
CREST_WARN = 4.0
KURT_WARN = 4.0

AXIS_NAMES = ["X", "Y", "Z"]

# TCS34725 color sensor settings
COLOR_AXIS_NAMES = ["R", "G", "B"]

# AXIS_COLORS, COLOR_AXIS_COLORS and LEVEL_COLOR now come from design_system so
# the plot series and health indicators use the CLAUDE.md palette.

# Color-cube detection -> automatic servo triggering.
# Runs continuously in the background, independent of the "Live" display checkbox.
COLOR_CUBE_THRESHOLD = 500

# Header logo target height in px (see ControlPanel._load_logo).
LOGO_HEIGHT = 30

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
# Demo mode: synthetic stand-ins for the hardware
#
# Every board read funnels through send_command(), so one replacement covers the
# servo, colour and temperature panels. Response strings match the real device
# protocol exactly, so the parsers and the cube-detection logic run unmodified.
# ---------------------------------------------------------------------------
_demo_t0 = None


def _demo_elapsed():
    global _demo_t0
    if _demo_t0 is None:
        _demo_t0 = time.monotonic()
    return time.monotonic() - _demo_t0


def demo_send_command(ip, cmd, timeout=TIMEOUT):
    """Protocol-accurate synthetic replies. Mirrors send_command's contract."""
    elapsed = _demo_elapsed()

    if cmd == "SERVO_STATUS":
        return "SERVO1:OFF,SERVO2:OFF"
    if cmd in ("SERVO1_ON", "SERVO1_OFF", "SERVO2_ON", "SERVO2_OFF"):
        return "OK"

    if cmd == "COLOR_READ":
        # 30 s loop: neutral -> red cube -> neutral -> green cube -> neutral.
        # Cube phases exceed COLOR_CUBE_THRESHOLD so _check_cube_trigger fires
        # the servo sequences; neutral phases stay well below it.
        t = elapsed % 30.0
        if 8.0 <= t < 12.0:
            r, g, b = 1800, 310, 290          # RED  -> SERVO1 sequence
        elif 20.0 <= t < 24.0:
            r, g, b = 280, 1600, 310          # GREEN -> SERVO2 sequence
        else:
            r = int(120 + 20 * math.sin(elapsed * 0.30))
            g = int(120 + 15 * math.sin(elapsed * 0.20 + 1.0))
            b = int(120 + 18 * math.sin(elapsed * 0.25 + 2.0))
        return f"R:{r},G:{g},B:{b}"

    if cmd == "READ_TEMP":
        return f"TEMP:{22.0 + 3.0 * math.sin(elapsed * 0.05):.1f}"

    return None


class _DemoChannel:
    """Stands in for max14906.channel['voltageN'] — accepts .raw writes."""
    raw = 0


class _DemoADIDevice:
    """Accepts the attribute writes the SWIOT1L panel performs on real devices."""

    def __init__(self, *_args, **_kwargs):
        self.channel = {f"voltage{i}": _DemoChannel() for i in range(4)}
        self.mode = "config"

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)

    def __getattr__(self, name):
        return 0


if args.demo:
    send_command = demo_send_command


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
class CardPanel(tk.Frame):
    """Base for every hardware panel: a themed Card with a title and a
    status row.

    Panels are plain tk.Frame (not ttk.LabelFrame) because ttk widgets ignore
    bg=/fg= and a LabelFrame's built-in title is only reachable through the
    global TLabelframe.Label style, which cannot be re-themed per panel at
    runtime. The Card supplies the border/shadow/padding from CLAUDE.md and the
    title becomes a real tk.Label we can recolour directly.
    """

    def __init__(self, parent, tm, title, subtitle=""):
        super().__init__(parent)
        self.tm = tm
        self._card = Card(self, tm, pad=MD)
        self._card.outer().pack(fill=tk.BOTH, expand=True)
        self.body = self._card.body

        self._title_lbl = tk.Label(self.body, text=title, anchor="w",
                                   font=tm.font_h2)
        self._title_lbl.pack(fill="x")
        self._subtitle_lbl = None
        if subtitle:
            self._subtitle_lbl = tk.Label(self.body, text=subtitle, anchor="w",
                                          font=tm.font_small)
            self._subtitle_lbl.pack(fill="x", pady=(0, SM))
        else:
            self._title_lbl.pack_configure(pady=(0, SM))

        # Widgets whose colours track the theme, registered by subclasses.
        self._plain_labels = []      # follow text/card
        self._muted_labels = []      # follow text2/card
        self._frames = []            # follow card bg

    # -- theme plumbing ----------------------------------------------------
    def register_theme(self):
        """Call at the end of __init__, once the UI exists."""
        self.tm.on_theme(self.apply_theme)

    def track(self, *widgets, muted=False):
        """Mark labels/frames to be recoloured on every theme switch.

        Always returns the first widget so it can be chained:
            self.track(tk.Label(...)).pack(...)
        """
        for w in widgets:
            if isinstance(w, tk.Label):
                (self._muted_labels if muted else self._plain_labels).append(w)
            else:
                self._frames.append(w)
        return widgets[0]

    def apply_theme(self):
        t = self.tm.theme
        self.configure(bg=t["bg"])
        self._title_lbl.configure(bg=t["card"], fg=t["text"])
        if self._subtitle_lbl is not None:
            self._subtitle_lbl.configure(bg=t["card"], fg=t["text_dis"])
        for w in self._frames:
            try:
                w.configure(bg=t["card"])
            except tk.TclError:
                pass
        for w in self._plain_labels:
            w.configure(bg=t["card"], fg=t["text"])
        for w in self._muted_labels:
            w.configure(bg=t["card"], fg=t["text2"])
        # The status label's fg encodes state, so only its bg follows the theme.
        if getattr(self, "status_label", None) is not None:
            self.status_label.configure(bg=t["card"],
                                        fg=STATUS_COLOR[self._status_key])
        self.style_plot()

    def style_plot(self):
        """Overridden by panels that embed a matplotlib figure."""

    def theme_axes(self, fig, ax, canvas):
        """Apply the CLAUDE.md palette to an embedded matplotlib axes."""
        t = self.tm.theme
        fig.patch.set_facecolor(t["card"])
        ax.set_facecolor(t["card"])
        ax.tick_params(colors=t["text2"], labelsize=7)
        for sp in ax.spines.values():
            sp.set_color(t["border"])
        ax.grid(True, color=t["border"], lw=0.5, alpha=0.6)
        ax.xaxis.label.set_color(t["text2"])
        ax.yaxis.label.set_color(t["text2"])
        ax.title.set_color(t["text"])
        leg = ax.get_legend()
        if leg is not None:
            for txt in leg.get_texts():
                txt.set_color(t["text2"])
        canvas.get_tk_widget().configure(bg=t["card"], highlightthickness=0)
        canvas.draw_idle()

    def status_row(self, label_text="Status:", value="Unknown",
                   button_text=None, button_cmd=None):
        """Build the shared 'Status: <value>  [Action]' row.

        The value is a tk.Label (not ttk) because its foreground carries state
        and a named ttk style would override per-widget colours.
        """
        row = tk.Frame(self.body)
        row.pack(fill="x", pady=(0, SM))
        self._frames.append(row)
        cap = tk.Label(row, text=label_text, font=self.tm.font_small)
        cap.pack(side="left")
        self._muted_labels.append(cap)
        self.status_label = tk.Label(row, text=value, font=self.tm.font_bold)
        self.status_label.pack(side="left", padx=(XS, 0))
        self._status_key = "idle"
        btn = None
        if button_text:
            btn = Button(row, self.tm, button_text, button_cmd,
                         variant="primary", height=28)
            btn.pack(side="right")
        return row, btn

    def set_status(self, text, key):
        """Set the status text and its semantic colour together.

        CLAUDE.md: colour is never the only indicator, so the wording changes
        alongside the colour.
        """
        self._status_key = key
        self.status_label.config(text=text, fg=STATUS_COLOR[key],
                                 bg=self.tm.theme["card"])


def rgb_counts_to_hex(r, g, b):
    """Normalize raw 16-bit ADC counts to a displayable sRGB swatch color."""
    peak = max(r, g, b, 1)
    scale = 255.0 / peak
    rr = min(int(r * scale), 255)
    gg = min(int(g * scale), 255)
    bb = min(int(b * scale), 255)
    return f"#{rr:02x}{gg:02x}{bb:02x}"


class ColorSensorPanel(CardPanel):
    """UI panel for an APARD board with a TCS34725 color sensor.

    A background thread continuously polls COLOR_READ over IP at
    COLOR_AUTO_REFRESH_MS (4 Hz) regardless of the "Live" checkbox, and
    checks every reading for a color cube (see _check_cube_trigger). The
    "Live" checkbox only controls whether those readings are reflected in
    the swatch/labels/graph.
    """

    def __init__(self, parent, tm, board_name, ip, log_callback, servo_panel=None,
                 shield=""):
        # The shield goes in the subtitle, not the title: board_name doubles as
        # the log-line prefix (see ControlPanel._log_message), so a long title
        # would push every other panel's log lines out of alignment.
        subtitle = " · ".join(p for p in (f"{shield} shield" if shield else "",
                                          "TCS34725 colour sensor", ip) if p)
        super().__init__(parent, tm, board_name, subtitle)
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
        # Gates cube detection -> servo actuation. Polling and the readout run
        # regardless; this only decides whether a detected cube is acted on, so
        # the sensor can be watched and calibrated before the line is armed.
        self._sorting_enabled = False
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
        self.register_theme()
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _build_ui(self):
        row, _ = self.status_row(button_text="Test", button_cmd=self._test_connection)

        # Packed side="right" after Test, so it lands left of it.
        # min_width keeps the card from reflowing when the label changes length.
        self._btn_sort = Button(row, self.tm, "Start Sorting", self._toggle_sorting,
                                variant="primary", height=28, min_width=110)
        self._btn_sort.pack(side="right", padx=(0, XS))

        # Colour is never the only indicator (CLAUDE.md): the wording carries the
        # state too, for the same reason set_status() changes both.
        self.sort_status_label = tk.Label(self.body, text="Sorting stopped",
                                          fg=NEUTRAL[500], font=self.tm.font_small,
                                          anchor="w")
        self.sort_status_label.pack(fill="x")
        self._frames.append(self.sort_status_label)   # bg only; fg carries state

        # No LabelFrame wrapper here (unlike the other panels): its title would
        # only repeat the card's own subtitle, and the border plus its internal
        # padding cost ~40px of height that the graph below needs more.
        readout = tk.Frame(self.body)
        readout.pack(fill="x", pady=(SM, 0))
        self._frames.append(readout)

        swatch_row = tk.Frame(readout)
        swatch_row.pack(fill="x")
        self._frames.append(swatch_row)

        self.swatch = tk.Canvas(swatch_row, width=60, height=38, bg=NEUTRAL[950],
                                highlightthickness=1)
        self.swatch.pack(side="left", padx=(0, SM))
        self.swatch_rect = self.swatch.create_rectangle(0, 0, 60, 38,
                                                        fill=NEUTRAL[950], outline="")

        # Packed before info_col so the expanding value column takes the slack
        # between the swatch and this label rather than pushing it off-panel.
        # Colour is paired with wording, per CLAUDE.md.
        self.cal_status_label = tk.Label(swatch_row, text="Not calibrated",
                                        fg=WARNING[500], font=self.tm.font_small,
                                        anchor="e")
        self.cal_status_label.pack(side="right", padx=(SM, 0))
        self._frames.append(self.cal_status_label)   # bg only; fg carries state

        info_col = tk.Frame(swatch_row)
        info_col.pack(side="left", fill="both", expand=True)
        self._frames.append(info_col)

        self.rgb_label = tk.Label(info_col, text="R: -   G: -   B: -",
                                  font=self.tm.font_mono_bold, anchor="w")
        self.rgb_label.pack(fill="x")
        self.hex_label = tk.Label(info_col, text="#------",
                                  font=self.tm.font_mono, anchor="w")
        self.hex_label.pack(fill="x")
        self.track(self.rgb_label)
        self.track(self.hex_label, muted=True)

        # One control row, not two: frequent actions left, occasional right.
        ctrl_row = tk.Frame(readout)
        ctrl_row.pack(fill="x", pady=(SM, 0))
        self._frames.append(ctrl_row)

        ttk.Button(ctrl_row, text="Read Color", style="DS.TButton",
                   command=self._read_color).pack(side="left")
        ttk.Checkbutton(ctrl_row, text="Live (4Hz)", style="DS.TCheckbutton",
                        variable=self.auto_refresh_var,
                        command=self._toggle_auto_refresh).pack(side="left", padx=SM)
        ttk.Button(ctrl_row, text="Clear", style="DS.TButton",
                   command=self._clear_graph).pack(side="right")
        ttk.Button(ctrl_row, text="Reset Cal", style="DS.TButton",
                   command=self._reset_calibration).pack(side="right", padx=(XS, SM))
        ttk.Button(ctrl_row, text="Calibrate White", style="DS.TButton",
                   command=self._calibrate_white).pack(side="right")

        if HAS_MATPLOTLIB:
            # constrained layout, matching ADXL355Panel — tight_layout() bakes
            # fractional margins at the initial figsize, which left this short
            # figure mostly dead space once the card grew.
            self.fig = Figure(figsize=(3.2, 2.4), dpi=80, layout="constrained")
            self.ax = self.fig.add_subplot(111)
            self.ax.set_xlabel("Time (s)", fontsize=8)
            self.ax.set_ylabel("Count", fontsize=8)
            self.lines = [
                self.ax.plot([], [], "-o", markersize=2, linewidth=1,
                             color=COLOR_AXIS_COLORS[i], label=COLOR_AXIS_NAMES[i])[0]
                for i in range(3)
            ]
            self.ax.legend(fontsize=7, loc="upper left", frameon=False)

            self.canvas = FigureCanvasTkAgg(self.fig, master=self.body)
            self.canvas.get_tk_widget().pack(fill="both", expand=True, pady=(SM, 0))

    def apply_theme(self):
        super().apply_theme()
        # No _group_frames loop: this panel has no LabelFrame, so every frame
        # and label is handled by the base class via self._frames.
        self.swatch.configure(highlightbackground=self.tm.theme["border"])

    def style_plot(self):
        if HAS_MATPLOTLIB:
            for i, line in enumerate(self.lines):
                line.set_color(COLOR_AXIS_COLORS[i])
            self.theme_axes(self.fig, self.ax, self.canvas)

    def _test_connection(self):
        def worker():
            resp = send_command(self.ip, "COLOR_READ")
            self.after(0, lambda: self._on_test_result(resp))
        threading.Thread(target=worker, daemon=True).start()

    def _on_test_result(self, resp):
        if resp is not None and resp.startswith("R:"):
            self.set_status("Reachable", "ok")
            self.log(self.board_name, f"Board reachable - {resp}")
            self._apply_reading(resp, record=False)
        else:
            self.set_status("Unreachable", "error")
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
            self.set_status("Reachable", "ok")
            self._apply_reading(resp, record=True)
        else:
            self.set_status("Unreachable", "error")

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
                self.set_status("Unreachable", "error")
            return

        self.set_status("Reachable", "ok")

        parsed = self._parse_reading(resp)
        if parsed is not None:
            self._check_cube_trigger(*parsed)

        if self.auto_refresh_var.get():
            self.log(self.board_name, f"<< {resp}")
            self._apply_reading(resp, record=True)

    def _toggle_sorting(self):
        """Arm or disarm cube detection -> servo actuation."""
        if self._sorting_enabled:
            self._stop_sorting()
        else:
            self._start_sorting()

    def _start_sorting(self):
        self._sorting_enabled = True
        # Clear the latch so a cube already sitting under the sensor when the
        # line is armed is picked up on the next poll rather than being treated
        # as already-handled.
        self._cube_color = None
        self._cube_handled = False
        self._cube_ignore_logged = False
        self._btn_sort.label = "Stop Sorting"
        self._btn_sort.variant = "secondary"
        self._btn_sort.render()
        self.sort_status_label.config(text="Sorting running", fg=SUCCESS[500])
        self.log(self.board_name, "Sorting started - cube detection armed")

    def _stop_sorting(self):
        self._sorting_enabled = False
        self._btn_sort.label = "Start Sorting"
        self._btn_sort.variant = "primary"
        self._btn_sort.render()
        self.sort_status_label.config(text="Sorting stopped", fg=NEUTRAL[500])
        self.log(self.board_name, "Sorting stopped - cube detection disarmed")

    def _check_cube_trigger(self, r, g, b):
        """Detect a color cube under the sensor from raw RGB counts and
        trigger the matching servo sequence. Runs on every poll."""
        # Detection is gated on the Start Sorting button, but an in-flight servo
        # sequence is left to finish on its own timers so a servo can't be
        # stranded in the ON position by disarming mid-cycle.
        if not self._sorting_enabled:
            return

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
        self.set_status("Unreachable", "error")

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
        self.cal_status_label.config(text="Calibrated", fg=SUCCESS[500])
        self.log(self.board_name,
                 f"White calibration set from R={ref_r} G={ref_g} B={ref_b} "
                 f"(gains: {self.cal_gains[0]:.2f}, {self.cal_gains[1]:.2f}, "
                 f"{self.cal_gains[2]:.2f})")

    def _reset_calibration(self):
        self.cal_gains = None
        self.cal_status_label.config(text="Not calibrated", fg=WARNING[500])
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


class ServoBoardPanel(CardPanel):
    """UI panel for an APARD board driving two servomotors."""

    def __init__(self, parent, tm, board_name, ip, log_callback, shield=""):
        # Shield in the subtitle, not the title -- see ColorSensorPanel.
        subtitle = " · ".join(p for p in (f"{shield} shield" if shield else "",
                                          "Servo controller", ip) if p)
        super().__init__(parent, tm, board_name, subtitle)
        self.board_name = board_name
        self.ip = ip
        self.log = log_callback
        self._build_ui()
        self.register_theme()

    def _build_ui(self):
        self.status_row(button_text="Test", button_cmd=self._test_connection)

        servo_frame = tk.LabelFrame(self.body, text="Servo Control", padx=SM, pady=SM)
        servo_frame.pack(fill="both", expand=True, pady=(SM, 0))
        self._group_frames = [servo_frame]
        self._frames.append(servo_frame)

        # Vertical layout: each servo's caption sits above its own ON/OFF pair,
        # rather than all three sharing one row. This panel has no plot, so it
        # takes the narrow column, where a label+2 buttons row would squeeze the
        # captions.
        for idx in (1, 2):
            block = tk.Frame(servo_frame)
            block.pack(fill="x", pady=(0, SM))
            self._frames.append(block)
            self.track(tk.Label(block, text=f"Servo {idx}:", anchor="w",
                                font=self.tm.font_ui), muted=True).pack(fill="x")

            btn_row = tk.Frame(block)
            btn_row.pack(fill="x", pady=(XS, 0))
            self._frames.append(btn_row)
            for state in ("ON", "OFF"):
                ttk.Button(btn_row, text=state, style="DS.TButton",
                           command=lambda i=idx, s=state: self._send(f"SERVO{i}_{s}")
                           ).pack(side="left", padx=(0, XS), expand=True, fill="x")

        ttk.Button(servo_frame, text="Servo Status", style="DS.TButton",
                   command=lambda: self._send("SERVO_STATUS")).pack(fill="x", pady=(XS, 0))

        self.servo1_state_label = tk.Label(servo_frame, text="Servo 1: --",
                                           font=self.tm.font_mono_bold, anchor="nw")
        self.servo1_state_label.pack(fill="x", pady=(SM, 0))
        self.servo2_state_label = tk.Label(servo_frame, text="Servo 2: --",
                                           font=self.tm.font_mono_bold, anchor="nw")
        # The last child absorbs the card's slack so there is no dead gap.
        self.servo2_state_label.pack(fill="both", expand=True)
        self.track(self.servo1_state_label, self.servo2_state_label)

    def apply_theme(self):
        super().apply_theme()
        t = self.tm.theme
        for f in self._group_frames:
            f.configure(bg=t["card"], fg=t["text2"],
                        highlightbackground=t["border"], bd=1, relief="solid")

    def _test_connection(self):
        def worker():
            resp = send_command(self.ip, "SERVO_STATUS")
            self.after(0, lambda: self._on_test_result(resp))
        threading.Thread(target=worker, daemon=True).start()

    def _on_test_result(self, resp):
        if resp is not None:
            self.set_status("Reachable", "ok")
            self.log(self.board_name, f"Board reachable at {self.ip}")
            self._parse_status(resp)
        else:
            self.set_status("Unreachable", "error")
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
        self.set_status("Reachable", "ok")
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
        self.set_status("Unreachable", "error")


class CN0575Panel(CardPanel):
    """UI panel for CN0575 with ADT75 temperature graph."""

    def __init__(self, parent, tm, log_callback):
        super().__init__(parent, tm, "CN0575", f"ADT75 temperature · {CN0575_IP}")
        self.log = log_callback
        self.ip = CN0575_IP
        self.board_name = "CN0575"
        self.temp_history = deque(maxlen=GRAPH_MAX_POINTS)
        self.time_history = deque(maxlen=GRAPH_MAX_POINTS)
        self.auto_refresh_var = tk.BooleanVar(value=False)
        self.auto_refresh_job = None
        self.start_time = None
        self._build_ui()
        self.register_theme()

    def _build_ui(self):
        self.status_row(button_text="Test", button_cmd=self._test_connection)

        # The readout shares the control row rather than owning a full-width row
        # of its own: that row plus its padding cost ~35px of chrome, and the
        # graph below is the panel's point. Same treatment as ColorSensorPanel.
        ctrl_frame = tk.Frame(self.body)
        ctrl_frame.pack(fill="x", pady=(SM, 0))
        self._frames.append(ctrl_frame)

        self.temp_label = tk.Label(ctrl_frame, text="ADT75 Temp: -- C",
                                   font=self.tm.font_readout, anchor="w")
        self.temp_label.pack(side="left")
        self.track(self.temp_label)

        # Packed right-to-left so the visual order stays Read Temp | Live | Clear.
        ttk.Button(ctrl_frame, text="Clear", style="DS.TButton",
                   command=self._clear_graph).pack(side="right")
        ttk.Checkbutton(ctrl_frame, text="Live (5s)", style="DS.TCheckbutton",
                        variable=self.auto_refresh_var,
                        command=self._toggle_auto_refresh).pack(side="right", padx=SM)
        ttk.Button(ctrl_frame, text="Read Temp", style="DS.TButton",
                   command=self._read_temp).pack(side="right")

        if HAS_MATPLOTLIB:
            # constrained layout, matching ColorSensorPanel/ADXL355Panel --
            # tight_layout() baked in fractional margins at the initial figsize
            # and left the axes only 54% of this short canvas.
            self.fig = Figure(figsize=(3.2, 2.0), dpi=80, layout="constrained")
            self.ax = self.fig.add_subplot(111)
            self.ax.set_xlabel("Time (s)", fontsize=8)
            self.ax.set_ylabel("Temp (C)", fontsize=8)
            self.line, = self.ax.plot([], [], "-o", markersize=2, linewidth=1,
                                      color=PRIMARY[500])

            self.canvas = FigureCanvasTkAgg(self.fig, master=self.body)
            self.canvas.get_tk_widget().pack(fill="both", expand=True, pady=(SM, 0))

    def style_plot(self):
        if HAS_MATPLOTLIB:
            self.line.set_color(PRIMARY[500] if self.tm.theme_name == "light"
                                else PRIMARY[300])
            self.theme_axes(self.fig, self.ax, self.canvas)

    def _test_connection(self):
        def worker():
            resp = send_command(self.ip, "READ_TEMP")
            self.after(0, lambda: self._on_test_result(resp))
        threading.Thread(target=worker, daemon=True).start()

    def _on_test_result(self, resp):
        if resp is not None and resp.startswith("TEMP:"):
            self.set_status("Reachable", "ok")
            self.log(self.board_name, f"Sensor reachable - {resp}")
            self.temp_label.config(text=f"ADT75 Temp: {resp[5:]} C")
        else:
            self.set_status("Unreachable", "error")
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
        self.set_status("Reachable", "ok")

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
        self.set_status("Unreachable", "error")

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


class SWIOT1LPanel(CardPanel):
    """UI panel for SWIOT1L fan PWM control."""

    def __init__(self, parent, tm, log_callback):
        super().__init__(parent, tm, "SWIOT1L", f"Fan PWM control · {SWIOT_IP}")
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
        self.register_theme()

    def _build_ui(self):
        _, self._btn_connect = self.status_row(value="Disconnected",
                                               button_text="Connect",
                                               button_cmd=self._connect)

        ctrl_frame = tk.LabelFrame(self.body, text="PWM Control", padx=SM, pady=SM)
        ctrl_frame.pack(fill="x", pady=(SM, 0))
        self._group_frames = [ctrl_frame]
        self._frames.append(ctrl_frame)

        dc_row = tk.Frame(ctrl_frame)
        dc_row.pack(fill="x")
        self._frames.append(dc_row)

        self.track(tk.Label(dc_row, text="Duty (%):", font=self.tm.font_ui),
                   muted=True).pack(side="left")
        self.dc_entry = ttk.Entry(dc_row, width=6, style="DS.TEntry")
        self.dc_entry.insert(0, "0")
        self.dc_entry.pack(side="left", padx=SM)

        ttk.Button(dc_row, text="Set", style="DS.TButton",
                   command=self._set_pwm).pack(side="left")
        ttk.Button(dc_row, text="Stop", style="DS.TButton",
                   command=self._stop_pwm).pack(side="left", padx=(XS, 0))

        self.dc_label = tk.Label(ctrl_frame, text="0% - 0 RPM",
                                 font=self.tm.font_mono_bold, anchor="w")
        self.dc_label.pack(fill="x", pady=(SM, 0))
        self.track(self.dc_label)

        if HAS_MATPLOTLIB:
            self.fig = Figure(figsize=(3.2, 1.5), dpi=80)
            self.ax = self.fig.add_subplot(111)
            self.ax.set_xlabel("Time (s)", fontsize=8)
            self.ax.set_ylabel("RPM", fontsize=8)
            self.line, = self.ax.plot([], [], "-o", markersize=2, linewidth=1,
                                      color=ERROR[500])
            self.fig.tight_layout()

            self.canvas = FigureCanvasTkAgg(self.fig, master=self.body)
            self.canvas.get_tk_widget().pack(fill="both", expand=True, pady=(SM, 0))

    def apply_theme(self):
        super().apply_theme()
        t = self.tm.theme
        for f in self._group_frames:
            f.configure(bg=t["card"], fg=t["text2"],
                        highlightbackground=t["border"], bd=1, relief="solid")

    def style_plot(self):
        if HAS_MATPLOTLIB:
            self.line.set_color(ERROR[500])
            self.theme_axes(self.fig, self.ax, self.canvas)

    def _connect(self):
        if not HAS_ADI and not args.demo:
            self.log(self.board_name, "pyadi-iio not installed")
            return

        self.set_status("Connecting...", "warn")
        self.log(self.board_name, "Connecting to SWIOT1L...")

        def worker():
            try:
                if args.demo:
                    # Synthetic stand-in: the RPM trace is derived from
                    # duty_cycle, not read back from the board, so the graph
                    # behaves exactly as it does on real hardware.
                    time.sleep(0.4)
                    self.max14906 = _DemoADIDevice()
                    self.connected = True
                    self.after(0, self._on_connected)
                    return
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
        self.set_status("Connected", "ok")
        self.log(self.board_name, "Connected - ready for PWM")

    def _on_connect_error(self, err):
        self.set_status("Error", "error")
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


class ADXL355Panel(CardPanel):
    """Compact ADXL355 predictive maintenance panel."""

    def __init__(self, parent, tm, log_callback, use_demo=False):
        host_info = "Demo" if use_demo else f"{args.adxl_host}:{args.adxl_port}"
        super().__init__(parent, tm, "ADXL355",
                         f"Vibration monitor · {host_info}")
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
        self.register_theme()

    def _build_ui(self):
        row, _ = self.status_row(value="Stopped")
        self._btn_start = Button(row, self.tm, "Start", self._start,
                                 variant="primary", height=28)
        self._btn_start.pack(side="right")
        self._btn_stop = Button(row, self.tm, "Stop", self._stop,
                                variant="secondary", height=28)
        self._btn_stop.pack(side="right", padx=(0, XS))
        self._btn_stop.set_enabled(False)
        # Brings up the servers this panel (and CN0575) read from, over SSH, so
        # the demo can be started without a terminal on the Pi.
        self._btn_servers = Button(row, self.tm, "Start Servers",
                                   self._start_servers,
                                   variant="secondary", height=28)
        self._btn_servers.pack(side="right", padx=(0, XS))

        # Axis status indicators. Now in the wide top row, so it can breathe.
        axis_frame = tk.LabelFrame(self.body, text="Axis Health", padx=SM, pady=XS)
        axis_frame.pack(fill="x", pady=(SM, 0))
        self._group_frames = [axis_frame]
        self._frames.append(axis_frame)

        self._status_canvases = []
        self._status_labels = []
        self._rms_labels = []

        for i, name in enumerate(AXIS_NAMES):
            row = tk.Frame(axis_frame)
            row.pack(fill="x", pady=1)
            self._frames.append(row)

            c = tk.Canvas(row, width=12, height=12, highlightthickness=0)
            c.pack(side="left", padx=(0, XS))
            oval = c.create_oval(1, 1, 11, 11, fill=SUCCESS[500], outline="")
            self._status_canvases.append((c, oval))
            self._frames.append(c)

            self.track(tk.Label(row, text=f"{name}:", width=3, anchor="w",
                                font=self.tm.font_ui), muted=True).pack(side="left")
            # The dot is never the sole indicator: this label spells out the level.
            sl = tk.Label(row, text="OK", width=8, anchor="w", fg=SUCCESS[500],
                          font=self.tm.font_small)
            sl.pack(side="left")
            self._status_labels.append(sl)
            self._frames.append(sl)

            rl = tk.Label(row, text="RMS: --", font=self.tm.font_mono)
            rl.pack(side="right")
            self._rms_labels.append(rl)
            self.track(rl, muted=True)

        # FFT plot (per-axis toggles + overlaid plot)
        if HAS_MATPLOTLIB:
            plot_ctrl = tk.Frame(self.body)
            plot_ctrl.pack(fill="x", pady=(SM, 0))
            self._frames.append(plot_ctrl)
            self.track(tk.Label(plot_ctrl, text="Show axes:", font=self.tm.font_ui),
                       muted=True).pack(side="left")
            # Checkbuttons, not Radiobuttons: the axes overlay so any subset can
            # be shown at once, which is what makes them comparable.
            self._axis_vars = [tk.BooleanVar(value=True) for _ in AXIS_NAMES]
            for i, name in enumerate(AXIS_NAMES):
                ttk.Checkbutton(plot_ctrl, text=name, style="DS.TCheckbutton",
                                variable=self._axis_vars[i],
                                command=self.style_plot).pack(side="left", padx=(XS, 0))

            # constrained layout (not a one-off tight_layout) so the axes keep
            # filling the card as it resizes — tight_layout bakes in fractional
            # margins at the initial figsize, leaving dead space once wider.
            # Measured full draw at 1500x1000: 28 ms/frame with one axis ticked,
            # 38 ms with all three, against the 100 ms budget at 10 Hz.
            self.fig = Figure(figsize=(5.0, 2.0), dpi=80, layout="constrained")
            self.ax = self.fig.add_subplot(111)
            self.ax.set_xlabel("Hz", fontsize=8)
            # No in-figure title: the card is already labelled, and the y-label
            # (single axis) or legend (several) says which trace is which.
            self.ax.set_ylabel("FFT (g)", fontsize=8)
            self.ax.set_xlim(0, ADXL_FREQ_MAX)
            self.lines = [
                self.ax.plot([], [], lw=1, color=AXIS_COLORS[i], label=name)[0]
                for i, name in enumerate(AXIS_NAMES)
            ]

            self.canvas = FigureCanvasTkAgg(self.fig, master=self.body)
            self.canvas.get_tk_widget().pack(fill="both", expand=True, pady=(SM, 0))

    def apply_theme(self):
        super().apply_theme()
        t = self.tm.theme
        for f in self._group_frames:
            f.configure(bg=t["card"], fg=t["text2"],
                        highlightbackground=t["border"], bd=1, relief="solid")

    def style_plot(self):
        """Recolour traces, apply the axis toggles, and label accordingly.

        Runs on a toggle or a theme switch, not per frame, so rebuilding the
        legend here is cheap.
        """
        if not HAS_MATPLOTLIB:
            return
        shown = []
        for i, line in enumerate(self.lines):
            line.set_color(AXIS_COLORS[i])
            visible = self._axis_vars[i].get()
            line.set_visible(visible)
            if visible:
                shown.append(line)

        # One axis needs no legend — the y-label names it, as before. Several
        # do, and the legend must exist before theme_axes recolours its text.
        leg = self.ax.get_legend()
        if leg is not None:
            leg.remove()
        if len(shown) == 1:
            self.ax.set_ylabel(f"FFT {shown[0].get_label()} (g)", fontsize=8)
        else:
            self.ax.set_ylabel("FFT (g)", fontsize=8)
            if shown:
                self.ax.legend(handles=shown, fontsize=7, loc="upper right",
                               frameon=False)

        self.theme_axes(self.fig, self.ax, self.canvas)

    # ------------------------------------------------------- remote servers
    def _start_servers(self):
        """Start both Pi-side servers over SSH, detached.

        Runs off the UI thread: ssh can block for SSH_CONNECT_TIMEOUT, which
        would freeze the window. Results come back via after(0, ...) like every
        other worker in this app.
        """
        self._btn_servers.set_enabled(False)
        self.log(self.board_name, f">> ssh {REMOTE_HOST}: starting servers")

        def worker():
            try:
                proc = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes",
                     "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
                     REMOTE_HOST, self._remote_launch_script()],
                    capture_output=True, text=True,
                    timeout=SSH_CONNECT_TIMEOUT + 10)
            except subprocess.TimeoutExpired:
                self.after(0, lambda: self._on_servers_done(
                    False, "ssh timed out (host unreachable?)"))
                return
            except OSError as exc:               # ssh binary missing
                self.after(0, lambda e=exc: self._on_servers_done(
                    False, f"cannot run ssh: {e}"))
                return
            ok = proc.returncode == 0
            detail = (proc.stdout or "").strip() or (proc.stderr or "").strip()
            self.after(0, lambda: self._on_servers_done(ok, detail))

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _remote_launch_script():
        """Shell script run on the Pi: one detached server per entry.

        setsid + nohup + closed stdin so each survives the SSH session closing
        (a plain '&' dies with the session). Each is skipped if already running,
        so the button is safe to press twice.
        """
        lines = [f"cd {REMOTE_DIR} || exit 1"]
        for _label, script, script_args, log in REMOTE_SERVERS:
            lines.append(
                f"pgrep -f {script} >/dev/null || "
                f"setsid nohup python3 {script} {script_args} "
                f">{REMOTE_LOG_DIR}/{log}.log 2>&1 </dev/null &"
            )
        # Give them a moment to bind, then report what is actually listening
        # rather than assuming the launch worked.
        lines.append("sleep 2")
        for _label, script, _a, _log in REMOTE_SERVERS:
            lines.append(f"pgrep -f {script} >/dev/null && echo UP:{script} "
                         f"|| echo DOWN:{script}")
        return "\n".join(lines)

    def _on_servers_done(self, ok, detail):
        self._btn_servers.set_enabled(True)
        down = [ln[5:] for ln in detail.splitlines() if ln.startswith("DOWN:")]
        up = [ln[3:] for ln in detail.splitlines() if ln.startswith("UP:")]
        if ok and up and not down:
            self.log(self.board_name, f"<< servers up: {', '.join(up)}")
            return
        if down:
            self.log(self.board_name, f"<< ERROR: failed to start {', '.join(down)}"
                                      f" (see {REMOTE_LOG_DIR}/*.log on the Pi)")
        else:
            # No UP/DOWN markers means the script never ran: auth or reachability.
            hint = ("SSH key auth not configured" if "denied" in detail.lower()
                    or "publickey" in detail.lower() else detail)
            self.log(self.board_name, f"<< ERROR: {hint or 'ssh failed'}")

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
            self.set_status("Running", "ok")
            self._btn_start.set_enabled(False)
            self._btn_stop.set_enabled(True)
            self.log(self.board_name, f"Started - FS={ADXL_FS}Hz")
            self._schedule_update()
        except Exception as e:
            self.set_status("Error", "error")
            self.log(self.board_name, f"Connection failed: {e}")

    def _stop(self):
        self._running = False
        if self.acq:
            self.acq.stop()
        if self._update_id:
            self.after_cancel(self._update_id)
            self._update_id = None
        self.set_status("Stopped", "idle")
        self._btn_start.set_enabled(True)
        self._btn_stop.set_enabled(False)
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

        # Update the FFT plot for every ticked axis, sharing one y-scale.
        if HAS_MATPLOTLIB:
            lo, hi = None, None
            for i, line in enumerate(self.lines):
                if not self._axis_vars[i].get():
                    continue
                samples = bufs[i]
                sig = (samples - samples.mean()) * self._window
                fft_mag = np.abs(np.fft.rfft(sig)) / self._win_norm
                mag = fft_mag[self._fmask]

                line.set_data(self._fdisp, mag)
                m_lo, m_hi = mag.min(), mag.max()
                lo = m_lo if lo is None else min(lo, m_lo)
                hi = m_hi if hi is None else max(hi, m_hi)

            # Untick everything and the limits simply stay put, rather than
            # autoscaling against an empty set.
            if lo is not None:
                pad = (hi - lo) * 0.15 + 1e-6
                self.ax.set_ylim(lo - pad, hi + pad)
            self.canvas.draw_idle()

    def cleanup(self):
        self._stop()


# ---------------------------------------------------------------------------
# Main Application Window
# ---------------------------------------------------------------------------
class ControlPanel(tk.Tk, ThemeMixin):
    """Main application window: header, panel rows, log and status bar."""

    def __init__(self):
        super().__init__()
        self.title("ADI DataX™ - AI-driven 10BASE-T1L Deployment")
        self.geometry("1500x1000")
        self.minsize(1200, 800)

        # Theme registry must exist before any panel or Button is constructed.
        self.init_theme(args.theme)
        fam = pick_font("Segoe UI", "Inter", "DejaVu Sans", "Helvetica")
        mono = pick_font("Cascadia Mono", "DejaVu Sans Mono", "Courier")
        self.font_ui         = tkfont.Font(family=fam,  size=9)
        self.font_small      = tkfont.Font(family=fam,  size=8, weight="bold")
        self.font_bold       = tkfont.Font(family=fam,  size=10, weight="bold")
        self.font_h1         = tkfont.Font(family=fam,  size=15, weight="bold")
        self.font_h2         = tkfont.Font(family=fam,  size=10, weight="bold")
        self.font_readout    = tkfont.Font(family=fam,  size=11, weight="bold")
        self.font_mono       = tkfont.Font(family=mono, size=8)
        self.font_mono_bold  = tkfont.Font(family=mono, size=9, weight="bold")

        self.style = ttk.Style()
        self.style.theme_use("clam")

        self._build_header()

        # Bottom chrome is packed BEFORE the expanding panel rows so it always
        # reserves its space — the panels request tall figures and would
        # otherwise squeeze the log and status bar off-screen entirely.
        self._build_statusbar()
        self._build_log()

        # The two panel rows live in a grid so their heights are governed by
        # weights, not by requested content size — with pack(), the top row's
        # tall colour figure starved the bottom row's three plots.
        rows = tk.Frame(self)
        rows.pack(side="top", fill="both", expand=True, padx=XL, pady=(MD, SM))
        rows.columnconfigure(0, weight=1)
        rows.rowconfigure(0, weight=1)     # APARD boards
        rows.rowconfigure(1, weight=1)     # CN0575 / SWIOT1L / ADXL355

        # Top row is 2-up (wide slots), bottom row 3-up (narrow slots).
        # ADXL355 sits top-left so its FFT gets a wide slot; the servo board,
        # which has no plot, takes the narrow bottom-right slot.
        top_frame = tk.Frame(rows)
        top_frame.grid(row=0, column=0, sticky="nsew", pady=(0, SM))
        for c in (0, 1):
            # uniform= as well as weight=: weight alone only divides the *surplus*
            # space, so the colour panel's wider control row would keep claiming
            # more than half. Same uniform group == identical column widths.
            top_frame.columnconfigure(c, weight=1, uniform="top")
        top_frame.rowconfigure(0, weight=1)

        middle_frame = tk.Frame(rows)
        middle_frame.grid(row=1, column=0, sticky="nsew", pady=(SM, 0))
        # The two plot panels share one uniform group so they stay identical;
        # the servo board has no plot, so it gets a narrower weight and a
        # vertical control layout to match. uniform= is what actually equalises
        # 0 and 1 -- weight alone only divides the surplus, and CN0575's readout
        # row makes it request more than SWIOT1L.
        for c in (0, 1):
            middle_frame.columnconfigure(c, weight=3, uniform="mid")
        middle_frame.columnconfigure(2, weight=2)
        middle_frame.rowconfigure(0, weight=1)

        # board1 is built first: ColorSensorPanel takes it as servo_panel so it
        # can drive the servo sequences on cube detection.
        self.board1 = ServoBoardPanel(middle_frame, self, "APARD32690 #1",
                                      APARD1_IP, self._log_message,
                                      shield="APARD-PFWD")
        self.board1.grid(row=0, column=2, sticky="nsew", padx=(MD // 2, 0))

        self.adxl355 = ADXL355Panel(top_frame, self, self._log_message,
                                    use_demo=args.demo)
        self.adxl355.grid(row=0, column=0, sticky="nsew", padx=(0, MD // 2))

        self.board2 = ColorSensorPanel(top_frame, self, "APARD32690 #2", APARD2_IP,
                                       self._log_message, servo_panel=self.board1,
                                       shield="APARD-SPoE")
        self.board2.grid(row=0, column=1, sticky="nsew", padx=(MD // 2, 0))

        # Every middle card carries the same total padx (MD // 2) so that equal
        # columns give equal *cards*: the uniform group equalises columns, and
        # card width = column width - padx, so mismatched padding would offset
        # the two panels the user wants identical. Split 4/4 on the middle card
        # keeps both gutters at 12 px.
        self.cn0575 = CN0575Panel(middle_frame, self, self._log_message)
        self.cn0575.grid(row=0, column=0, sticky="nsew", padx=(0, MD // 2))

        self.swiot = SWIOT1LPanel(middle_frame, self, self._log_message)
        self.swiot.grid(row=0, column=1, sticky="nsew", padx=(MD // 4, MD // 4))

        self._frames = [rows, top_frame, middle_frame,
                        self._log_host, self._log_head]

        self.apply_theme()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ shell
    def _build_log(self):
        """Communication log in a Card, matching the panels."""
        self._log_host = tk.Frame(self)
        self._log_host.pack(side="bottom", fill="x", padx=XL, pady=SM)
        self._log_card = Card(self._log_host, self, pad=MD)
        self._log_card.outer().pack(fill="both", expand=True)

        self._log_head = tk.Frame(self._log_card.body)
        self._log_head.pack(fill="x", pady=(0, SM))
        self._log_title = tk.Label(self._log_head, text="Communication Log",
                                   font=self.font_h2, anchor="w")
        self._log_title.pack(side="left")

        self._btn_test_all = Button(self._log_head, self, "Test All", self._test_all,
                                    variant="primary", height=28)
        self._btn_test_all.pack(side="right")
        self._btn_clear = Button(self._log_head, self, "Clear Log", self._clear_log,
                                 variant="secondary", height=28)
        self._btn_clear.pack(side="right", padx=(0, SM))

        self.log_text = scrolledtext.ScrolledText(
            self._log_card.body, height=6, state="disabled",
            font=self.font_mono, bd=0, relief="flat", highlightthickness=1,
        )
        self.log_text.pack(fill="both", expand=True)

    def _load_logo(self, height):
        """Load the header logo, scaled to `height` px, or None if unavailable.

        Reads assets/adi_logo.png (white artwork on transparency, generated
        from the official blue-on-white lockup). With Pillow the image is
        resized smoothly and an opaque black backdrop is converted to
        transparency; without it, Tk's integer-only subsample is used.
        """
        here = os.path.dirname(os.path.abspath(__file__))
        for fname in ("adi_logo.png",):
            path = os.path.join(here, "assets", fname)
            if not os.path.exists(path):
                continue
            try:
                from PIL import Image, ImageTk
            except ImportError:
                try:
                    img = tk.PhotoImage(file=path)
                except tk.TclError:
                    continue
                if img.height() > height:        # integer downscale only
                    img = img.subsample(max(1, round(img.height() / height)))
                return img
            try:
                im = Image.open(path).convert("RGBA")
                if im.getextrema()[3][0] == 255:          # fully opaque
                    lum = im.convert("L")
                    if sum(lum.crop((0, 0, 1, 1)).getdata()) < 32:   # black corner
                        im = Image.merge("RGBA", (*Image.new("RGB", im.size,
                                                             (255, 255, 255)).split(),
                                                  lum))
                bbox = im.split()[3].getbbox()
                if bbox:
                    im = im.crop(bbox)                    # trim padding
                w = max(1, round(im.width * height / im.height))
                return ImageTk.PhotoImage(im.resize((w, height), Image.LANCZOS))
            except Exception:
                continue
        return None

    def _build_header(self):
        head = tk.Frame(self, height=XXL + MD)
        head.pack(side="top", fill="x")
        head.pack_propagate(False)

        left = tk.Frame(head)
        left.pack(side="left", padx=XL)

        # Analog Devices logo. Drop a file at assets/adi_logo.png (ideally white
        # artwork on a transparent background) and it appears here automatically;
        # otherwise the triangle mark, then a glyph, are used as fallbacks.
        self._logo_img = self._load_logo(LOGO_HEIGHT)
        if self._logo_img is not None:
            logo = tk.Label(left, image=self._logo_img, bd=0)
        else:
            logo = tk.Label(left, text="◆", font=("", 16))   # last-resort glyph
        logo.pack(side="left", padx=(0, MD))
        name = tk.Label(left, text="ADI DataX™ - AI-driven 10BASE-T1L Deployment",
                        font=self.font_h1)
        name.pack(side="left")

        right = tk.Frame(head)
        right.pack(side="right", padx=XL)

        self._btn_theme = Button(right, self, "Dark theme", self._on_toggle_theme,
                                 variant="secondary", icon="◐", height=30)
        self._btn_theme.pack(side="left")

        self._header_widgets = (head, left, right)
        self._header_labels = (logo, name)

    def _build_statusbar(self):
        bar = tk.Frame(self, height=XL)
        bar.pack(side="bottom", fill="x")
        bar.pack_propagate(False)

        self._status_dot = tk.Label(bar, text="●", font=self.font_small)
        self._status_dot.pack(side="left", padx=(XL, XS))
        # Colour is paired with wording so the dot is never the sole indicator.
        self._status_txt = tk.Label(bar, text="Status: Ready", font=self.font_small)
        self._status_txt.pack(side="left")
        mode = "Demo mode — synthetic data" if args.demo else "Live hardware"
        self._mode_txt = tk.Label(bar, text=mode, font=self.font_small)
        self._mode_txt.pack(side="left", padx=XL)
        self._refresh_txt = tk.Label(bar, text="Last Refresh: --", font=self.font_mono)
        self._refresh_txt.pack(side="right", padx=XL)
        self._statusbar = bar

    # ----------------------------------------------------------------- theming
    def _on_toggle_theme(self):
        self.toggle_theme()
        self._log_message("UI", f"Switched to {self.theme_name} theme")

    def apply_theme(self):
        t = self.theme
        self.configure(bg=t["bg"])
        self._btn_theme.label = ("Dark theme" if self.theme_name == "light"
                                else "Light theme")
        self._btn_theme.icon = "◐" if self.theme_name == "light" else "◑"
        self._apply_ttk_styles()

        for w in self._header_widgets:
            w.configure(bg=t["header_bg"])
        for w in self._header_labels:
            # The logo label holds an image, so only its bg matters; fg would
            # be a no-op there but is what tints the wordmark text.
            w.configure(bg=t["header_bg"])
            if not w.cget("image"):
                w.configure(fg=t["header_fg"])
        for w in getattr(self, "_frames", []):
            w.configure(bg=t["bg"] if w not in (self._log_card.body,) else t["card"])
        self._log_title.configure(bg=t["card"], fg=t["text"])
        self.log_text.configure(bg=t["surface"], fg=t["text"],
                                insertbackground=t["text"],
                                highlightbackground=t["border"],
                                highlightcolor=t["border"],
                                selectbackground=t["primary"],
                                selectforeground=t["on_primary"])
        self._statusbar.configure(bg=t["surface"])
        self._status_dot.configure(bg=t["surface"], fg=SUCCESS[500])
        for w in (self._status_txt, self._mode_txt):
            w.configure(bg=t["surface"], fg=t["text2"])
        self._refresh_txt.configure(bg=t["surface"], fg=t["text_dis"])

        # ScrolledText embeds a classic tk.Scrollbar in its own frame.
        for w in (self.log_text.master, *self.log_text.master.winfo_children()):
            if isinstance(w, tk.Scrollbar):
                w.configure(bg=t["surface"], troughcolor=t["bg"],
                            activebackground=t["primary"], bd=0,
                            highlightthickness=0)
            elif isinstance(w, tk.Frame):
                w.configure(bg=t["card"])

        # Run every panel/Button/Card hook, then flush so ttk repaints at once.
        super().apply_theme()

        # Buttons paint their canvas with the *parent's* bg, and hooks run in
        # construction order — a Button registered before its parent frame was
        # themed would keep the stale bg. Re-render them once the tree is done.
        self._rerender_buttons(self)
        self.update_idletasks()

    def _rerender_buttons(self, widget):
        for child in widget.winfo_children():
            if isinstance(child, Button):
                child.render()
            else:
                self._rerender_buttons(child)

    def _apply_ttk_styles(self):
        """ttk widgets ignore bg=/fg=, so they are driven by named styles."""
        t, s = self.theme, self.style
        # Secondary buttons need a visible surface, so they sit on "surface"
        # with a border rather than blending into the card behind them.
        s.configure("DS.TButton", background=t["surface"], foreground=t["text"],
                    bordercolor=t["border"], lightcolor=t["border"],
                    darkcolor=t["border"], focuscolor=t["primary"],
                    relief="solid", borderwidth=1,
                    padding=(SM, XS), font=self.font_ui, anchor="center")
        s.map("DS.TButton",
              background=[("pressed", t["primary"]), ("active", t["row_hover"]),
                          ("disabled", t["surface"])],
              foreground=[("pressed", t["on_primary"]),
                          ("disabled", t["text_dis"])],
              bordercolor=[("focus", t["primary"]), ("active", t["primary"])])
        for cls in ("DS.TCheckbutton", "DS.TRadiobutton"):
            s.configure(cls, background=t["card"], foreground=t["text2"],
                        indicatorcolor=t["surface"], bordercolor=t["border"],
                        focuscolor=t["primary"], font=self.font_ui)
            s.map(cls,
                  background=[("active", t["card"])],
                  foreground=[("active", t["text"])],
                  indicatorcolor=[("selected", t["primary"])])
        s.configure("DS.TEntry", fieldbackground=t["surface"],
                    foreground=t["text"], bordercolor=t["border"],
                    lightcolor=t["border"], darkcolor=t["border"],
                    insertcolor=t["text"], padding=XS)
        s.configure("DS.TFrame", background=t["card"])
        s.configure("DS.Vertical.TScrollbar", background=t["surface"],
                    troughcolor=t["bg"], bordercolor=t["border"],
                    arrowcolor=t["text2"])

    # -------------------------------------------------------------- log/status
    def _log_message(self, board_name, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        entry = f"[{timestamp}] {board_name}: {message}\n"
        self.log_text.config(state="normal")
        self.log_text.insert("end", entry)
        self.log_text.see("end")
        self.log_text.config(state="disabled")
        self._refresh_txt.config(text=f"Last Refresh: {timestamp}")

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

