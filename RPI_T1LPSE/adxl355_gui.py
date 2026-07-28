#!/usr/bin/env python3
"""
ADXL355 Predictive Maintenance Monitor
Tkinter GUI with embedded live matplotlib plots.

Requirements:
    pip install pyadi-iio matplotlib numpy

Usage:
    python3 adxl355_gui.py                  # real hardware
    python3 adxl355_gui.py --demo           # synthetic data (no hardware needed)
    python3 adxl355_gui.py --rate 4000 --chunk 128
"""

import argparse
import json
import socket
import struct
import threading
import time
from datetime import datetime
import numpy as np
import tkinter as tk
from tkinter import ttk, scrolledtext
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser()
parser.add_argument("--rate",  type=int,   default=1000)
parser.add_argument("--buf",   type=int,   default=2048,  help="FFT window (power of 2)")
parser.add_argument("--chunk", type=int,   default=256,   help="Samples per acquisition call")
parser.add_argument("--range", type=float, default=500,   help="Max display frequency (Hz)")
parser.add_argument("--db",    action="store_true",       help="dB FFT scale")
parser.add_argument("--demo",  action="store_true",       help="Synthetic demo data")
parser.add_argument("--host",  type=str,   default="localhost", help="Server IP/hostname")
parser.add_argument("--port",  type=int,   default=50055,       help="Server TCP port")
args = parser.parse_args()

FS       = args.rate
FFT_WIN  = args.buf
CHUNK    = args.chunk
FREQ_MAX = min(args.range, FS / 2)
DB_SCALE = args.db
M_S2_TO_G = 1.0 / 9.80665

# ---------------------------------------------------------------------------
# Predictive maintenance thresholds (RMS in g)
# ---------------------------------------------------------------------------
THRESH_WARN  = 0.10   # g — editable at runtime
THRESH_ALARM = 0.50   # g
CREST_WARN   = 4.0    # crest factor
KURT_WARN    = 4.0    # kurtosis (healthy signal ≈ 3)

AXIS_NAMES = ["X", "Y", "Z"]
COLORS     = ["#1f77b4", "#ff7f0e", "#2ca02c"]

# ---------------------------------------------------------------------------
# Demo data source
# ---------------------------------------------------------------------------
class DemoSource:
    """Generates synthetic vibration: 50 Hz fundamental + harmonics + noise."""
    def __init__(self):
        self._t = 0.0
        self._fault = False
        self._fault_timer = 0

    def read_chunk(self, n):
        t = np.linspace(self._t, self._t + n / FS, n, endpoint=False)
        self._t += n / FS
        data = []
        for axis_i in range(3):
            freq = [50, 100, 150][axis_i]
            amp  = [0.05, 0.03, 0.02][axis_i]
            sig  = amp * np.sin(2 * np.pi * freq * t)
            sig += 0.01 * np.random.randn(n)
            # Inject periodic impulsive fault on X
            if axis_i == 0:
                self._fault_timer += n
                if self._fault_timer > FS * 8:
                    self._fault_timer = 0
                    self._fault = True
                if self._fault:
                    idx = np.random.randint(0, n)
                    sig[idx] += 0.8 * np.random.choice([-1, 1])
                    self._fault = False
            data.append(sig * 9.80665)   # back to m/s² to match real device
        return data

# ---------------------------------------------------------------------------
# TCP client source — connects to adxl355_server.py
# ---------------------------------------------------------------------------
class TCPSource:
    def __init__(self, host, port):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.connect((host, port))
        self._sock.settimeout(10.0)
        cfg = json.loads(self._recv_msg())
        self.fs    = cfg["fs"]
        self.chunk = cfg["chunk"]
        print(f"Server config: fs={self.fs} Hz  chunk={self.chunk}")

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

# ---------------------------------------------------------------------------
# ADXL355 acquisition thread
# ---------------------------------------------------------------------------
class Acquisition:
    def __init__(self, source):
        self._source  = source
        self._buf     = [np.zeros(FFT_WIN) for _ in range(3)]
        self._lock    = threading.Lock()
        self._running = False
        self._thread  = None

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
                raw = self._source.read_chunk(CHUNK)
            except Exception as exc:
                print(f"Acquisition error: {exc}")
                time.sleep(0.1)
                continue
            with self._lock:
                for i in range(3):
                    chunk = np.asarray(raw[i], dtype=np.float64) * M_S2_TO_G
                    self._buf[i] = np.roll(self._buf[i], -len(chunk))
                    self._buf[i][-len(chunk):] = chunk

# ---------------------------------------------------------------------------
# Metrics helper
# ---------------------------------------------------------------------------
def compute_metrics(samples):
    ac   = samples - samples.mean()
    rms  = float(np.sqrt(np.mean(ac ** 2)))
    peak = float(np.abs(ac).max())
    cf   = peak / rms if rms > 1e-9 else 0.0
    std  = ac.std()
    kurt = float(np.mean((ac / std) ** 4)) if std > 1e-9 else 3.0
    return rms, peak, cf, kurt

def health_level(rms, cf, kurt, warn_thr, alarm_thr):
    if rms >= alarm_thr or cf >= CREST_WARN * 1.5 or kurt >= KURT_WARN * 2:
        return "ALARM"
    if rms >= warn_thr or cf >= CREST_WARN or kurt >= KURT_WARN:
        return "WARNING"
    return "OK"

LEVEL_COLOR = {"OK": "#2ecc71", "WARNING": "#f39c12", "ALARM": "#e74c3c"}

# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root, acq):
        self.root  = root
        self.acq   = acq
        self._running = False
        self._prev_level = ["OK"] * 3
        self._prev_peak_freq = [0.0] * 3
        self._update_id = None

        root.title("ADXL355 — Predictive Maintenance Monitor")
        root.configure(bg="#2b2b2b")
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        # FFT helpers
        self._window   = np.hanning(FFT_WIN)
        self._win_norm = self._window.sum()
        freqs          = np.fft.rfftfreq(FFT_WIN, d=1.0 / FS)
        self._fmask    = freqs <= FREQ_MAX
        self._fdisp    = freqs[self._fmask]
        self._xsamp    = np.arange(FFT_WIN)

        self._build_ui()
        self._log(f"Ready. FS={FS} Hz | FFT window={FFT_WIN} | chunk={CHUNK}")
        self._log(f"Freq resolution: {FS/FFT_WIN:.3f} Hz/bin | "
                  f"Latency: ~{1000*CHUNK/FS:.0f} ms")

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = self.root

        # ── Left: plots ──────────────────────────────────────────────────
        plot_frame = tk.Frame(root, bg="#2b2b2b")
        plot_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.fig = Figure(figsize=(10, 7), facecolor="#1e1e1e")
        self.fig.subplots_adjust(hspace=0.45, wspace=0.35)
        self._axs_t, self._axs_f = [], []
        self._lines_t, self._lines_f = [], []

        for i, (name, color) in enumerate(zip(AXIS_NAMES, COLORS)):
            at = self.fig.add_subplot(3, 2, i * 2 + 1)
            af = self.fig.add_subplot(3, 2, i * 2 + 2)
            for ax in (at, af):
                ax.set_facecolor("#252525")
                ax.tick_params(colors="#aaa", labelsize=7)
                for sp in ax.spines.values():
                    sp.set_color("#444")
                ax.grid(True, color="#333", lw=0.5)

            at.set_title(f"{name} – Time domain", color="#ccc", fontsize=8)
            at.set_ylabel("g", color="#aaa", fontsize=7)
            at.set_xlim(0, FFT_WIN - 1)
            at.set_ylim(-2, 2)
            lt, = at.plot([], [], lw=0.6, color=color)

            af.set_title(f"{name} – FFT", color="#ccc", fontsize=8)
            af.set_ylabel("dB" if DB_SCALE else "g", color="#aaa", fontsize=7)
            af.set_xlim(0, FREQ_MAX)
            lf, = af.plot([], [], lw=0.8, color=color)

            self._axs_t.append(at);  self._axs_f.append(af)
            self._lines_t.append(lt); self._lines_f.append(lf)

        self._axs_t[-1].set_xlabel("Sample", color="#aaa", fontsize=7)
        self._axs_f[-1].set_xlabel("Hz",     color="#aaa", fontsize=7)

        canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
        canvas.draw()
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self._canvas = canvas

        # ── Right: control panel ─────────────────────────────────────────
        ctrl = tk.Frame(root, bg="#2b2b2b", width=280)
        ctrl.pack(side=tk.RIGHT, fill=tk.Y, padx=6, pady=6)
        ctrl.pack_propagate(False)

        # Title
        tk.Label(ctrl, text="ADXL355 Monitor", font=("Helvetica", 12, "bold"),
                 bg="#2b2b2b", fg="white").pack(pady=(4, 8))

        # ── Buttons ──
        btn_frame = tk.Frame(ctrl, bg="#2b2b2b")
        btn_frame.pack(fill=tk.X, pady=4)

        self._btn_start = ttk.Button(btn_frame, text="▶  Start",
                                     command=self._start, width=11)
        self._btn_start.pack(side=tk.LEFT, padx=4)

        self._btn_stop = ttk.Button(btn_frame, text="■  Stop",
                                    command=self._stop, width=11, state=tk.DISABLED)
        self._btn_stop.pack(side=tk.LEFT, padx=4)

        ttk.Button(ctrl, text="🗑  Clear log", command=self._clear_log).pack(pady=2)

        ttk.Separator(ctrl, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=8)

        # ── Axis status indicators ──
        tk.Label(ctrl, text="AXIS STATUS", font=("Helvetica", 9, "bold"),
                 bg="#2b2b2b", fg="#aaa").pack()

        self._status_canvases = []
        self._status_labels   = []
        self._rms_labels      = []

        for name in AXIS_NAMES:
            row = tk.Frame(ctrl, bg="#2b2b2b")
            row.pack(fill=tk.X, padx=8, pady=2)

            c = tk.Canvas(row, width=14, height=14, bg="#2b2b2b",
                          highlightthickness=0)
            c.pack(side=tk.LEFT, padx=(0, 6))
            oval = c.create_oval(2, 2, 12, 12, fill="#2ecc71", outline="")
            self._status_canvases.append((c, oval))

            tk.Label(row, text=f"{name}:", width=3, anchor="w",
                     bg="#2b2b2b", fg="white", font=("Courier", 9)).pack(side=tk.LEFT)
            sl = tk.Label(row, text="OK", width=8, anchor="w",
                          bg="#2b2b2b", fg="#2ecc71", font=("Courier", 9, "bold"))
            sl.pack(side=tk.LEFT)
            self._status_labels.append(sl)

            rl = tk.Label(row, text="RMS -", anchor="e",
                          bg="#2b2b2b", fg="#888", font=("Courier", 8))
            rl.pack(side=tk.RIGHT)
            self._rms_labels.append(rl)

        ttk.Separator(ctrl, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=8)

        # ── Thresholds ──
        tk.Label(ctrl, text="THRESHOLDS (RMS g)", font=("Helvetica", 9, "bold"),
                 bg="#2b2b2b", fg="#aaa").pack()

        thr_frame = tk.Frame(ctrl, bg="#2b2b2b")
        thr_frame.pack(fill=tk.X, padx=8, pady=4)

        tk.Label(thr_frame, text="Warn:",  bg="#2b2b2b", fg="#f39c12",
                 font=("Courier", 9), width=6, anchor="w").grid(row=0, column=0)
        self._entry_warn = ttk.Entry(thr_frame, width=7)
        self._entry_warn.insert(0, str(THRESH_WARN))
        self._entry_warn.grid(row=0, column=1, padx=4)

        tk.Label(thr_frame, text="Alarm:", bg="#2b2b2b", fg="#e74c3c",
                 font=("Courier", 9), width=6, anchor="w").grid(row=1, column=0, pady=2)
        self._entry_alarm = ttk.Entry(thr_frame, width=7)
        self._entry_alarm.insert(0, str(THRESH_ALARM))
        self._entry_alarm.grid(row=1, column=1, padx=4)

        ttk.Button(ctrl, text="Apply thresholds",
                   command=self._apply_thresholds).pack(pady=4)

        ttk.Separator(ctrl, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=8)

        # ── Message log ──
        tk.Label(ctrl, text="DIAGNOSTIC LOG", font=("Helvetica", 9, "bold"),
                 bg="#2b2b2b", fg="#aaa").pack()

        self._log_box = scrolledtext.ScrolledText(
            ctrl, height=14, bg="#111", fg="#ccc",
            font=("Courier", 8), wrap=tk.WORD,
            insertbackground="white", relief=tk.FLAT,
            state=tk.DISABLED,
        )
        self._log_box.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self._log_box.tag_config("OK",      foreground="#2ecc71")
        self._log_box.tag_config("WARNING", foreground="#f39c12")
        self._log_box.tag_config("ALARM",   foreground="#e74c3c")
        self._log_box.tag_config("INFO",    foreground="#5dade2")

        # ── Bottom status bar ──
        self._statusbar = tk.Label(
            root, text="Stopped", anchor=tk.W,
            bg="#1a1a1a", fg="#888", font=("Courier", 8), padx=6,
        )
        self._statusbar.pack(side=tk.BOTTOM, fill=tk.X)

    # ---------------------------------------------------------------- Controls
    def _start(self):
        self._running = True
        self.acq.start()
        self._btn_start.config(state=tk.DISABLED)
        self._btn_stop.config(state=tk.NORMAL)
        self._statusbar.config(text=f"Running  |  {FS} Hz  |  FFT {FFT_WIN} pts  "
                                     f"|  {FS/FFT_WIN:.2f} Hz/bin")
        self._log("Acquisition started.", tag="INFO")
        self._schedule_update()

    def _stop(self):
        self._running = False
        self.acq.stop()
        if self._update_id:
            self.root.after_cancel(self._update_id)
            self._update_id = None
        self._btn_start.config(state=tk.NORMAL)
        self._btn_stop.config(state=tk.DISABLED)
        self._statusbar.config(text="Stopped")
        self._log("Acquisition stopped.", tag="INFO")

    def _clear_log(self):
        self._log_box.config(state=tk.NORMAL)
        self._log_box.delete("1.0", tk.END)
        self._log_box.config(state=tk.DISABLED)

    def _apply_thresholds(self):
        global THRESH_WARN, THRESH_ALARM
        try:
            w = float(self._entry_warn.get())
            a = float(self._entry_alarm.get())
            if 0 < w < a:
                THRESH_WARN, THRESH_ALARM = w, a
                self._log(f"Thresholds updated: warn={w}g  alarm={a}g", tag="INFO")
            else:
                self._log("Invalid thresholds (must be 0 < warn < alarm).", tag="WARNING")
        except ValueError:
            self._log("Invalid threshold values.", tag="WARNING")

    def _on_close(self):
        self._stop()
        self.root.destroy()

    # ---------------------------------------------------------------- Logging
    def _log(self, msg, tag="INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self._log_box.config(state=tk.NORMAL)
        self._log_box.insert(tk.END, line, tag)
        self._log_box.see(tk.END)
        self._log_box.config(state=tk.DISABLED)

    # ---------------------------------------------------------------- Update loop
    def _schedule_update(self):
        if self._running:
            self._update()
            self._update_id = self.root.after(100, self._schedule_update)

    def _update(self):
        bufs = self.acq.snapshot()

        for i, (samples, color) in enumerate(zip(bufs, COLORS)):
            rms, peak, cf, kurt = compute_metrics(samples)
            level = health_level(rms, cf, kurt, THRESH_WARN, THRESH_ALARM)

            # Status indicators
            c, oval = self._status_canvases[i]
            col = LEVEL_COLOR[level]
            c.itemconfig(oval, fill=col)
            self._status_labels[i].config(text=level, fg=col)
            self._rms_labels[i].config(
                text=f"RMS {rms:.3f}g  CF {cf:.1f}  K {kurt:.1f}"
            )

            # Diagnostic messages on level change
            if level != self._prev_level[i]:
                self._log(
                    f"{AXIS_NAMES[i]}: {self._prev_level[i]} → {level}  "
                    f"(RMS={rms:.3f}g  CF={cf:.1f}  K={kurt:.1f})",
                    tag=level,
                )
                self._prev_level[i] = level

            # Crest factor / kurtosis warnings
            if cf >= CREST_WARN and level == "WARNING":
                self._log(
                    f"{AXIS_NAMES[i]}: High crest factor {cf:.1f} — "
                    "possible impulsive fault (bearing spalling?)", tag="WARNING"
                )
            if kurt >= KURT_WARN:
                self._log(
                    f"{AXIS_NAMES[i]}: Kurtosis={kurt:.1f} — "
                    "early bearing defect indicator", tag="WARNING"
                )

            # Peak frequency shift
            sig     = (samples - samples.mean()) * self._window
            fft_mag = np.abs(np.fft.rfft(sig)) / self._win_norm
            pk_idx  = np.argmax(fft_mag[self._fmask][1:]) + 1
            pk_freq = self._fdisp[pk_idx]
            if abs(pk_freq - self._prev_peak_freq[i]) > 5.0:
                self._log(
                    f"{AXIS_NAMES[i]}: dominant freq {self._prev_peak_freq[i]:.1f}"
                    f" → {pk_freq:.1f} Hz", tag="INFO"
                )
                self._prev_peak_freq[i] = pk_freq

            # ── Time domain ──
            self._lines_t[i].set_data(self._xsamp, samples)
            lim = max(np.abs(samples).max() * 1.3, 0.05)
            self._axs_t[i].set_ylim(-lim, lim)

            # ── FFT ──
            mag = fft_mag[self._fmask].copy()
            if DB_SCALE:
                mag = 20.0 * np.log10(np.maximum(mag, 1e-9))
            self._lines_f[i].set_data(self._fdisp, mag)
            pad = (mag.max() - mag.min()) * 0.15 + 1e-6
            self._axs_f[i].set_ylim(mag.min() - pad, mag.max() + pad)

        self._canvas.draw_idle()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    if args.demo:
        print("Running in DEMO mode (synthetic data)")
        source = DemoSource()

        class _DemoAdaptor:
            def __init__(self, src): self._src = src
            def read_chunk(self, n): return self._src.read_chunk(n)

        acq = Acquisition(_DemoAdaptor(source))
    else:
        try:
            print(f"Connecting to server {args.host}:{args.port} ...")
            source = TCPSource(args.host, args.port)
            global FS, CHUNK, FREQ_MAX
            FS       = source.fs
            CHUNK    = source.chunk
            FREQ_MAX = min(args.range, FS / 2)
            acq = Acquisition(source)
        except Exception as e:
            print(f"Could not connect to server: {e}")
            print("Tip: start adxl355_server.py on the sensor Pi, or use --demo")
            return

    root = tk.Tk()
    root.geometry("1280x750")
    App(root, acq)
    root.mainloop()

if __name__ == "__main__":
    main()