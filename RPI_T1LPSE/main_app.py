#!/usr/bin/env python3
"""
Industrial Demo Control Panel 

Run on the main Raspberry Pi:
    python3 main_app.py

Requires matplotlib for the CN0575 temperature graph:
    pip3 install matplotlib
"""

import tkinter as tk
from tkinter import ttk, scrolledtext
import socket
import threading
from datetime import datetime
from collections import deque
import time

try:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False
    print("Warning: matplotlib not found. CN0575 graph will be disabled.")
    print("Install with: pip3 install matplotlib")

try:
    import adi
    HAS_ADI = True
except ImportError:
    HAS_ADI = False
    print("Warning: pyadi-iio not found. SWIOT1L panel will be disabled.")
    print("Install with: pip3 install pyadi-iio")


APARD1_IP = "192.168.98.50"
APARD2_IP = "192.168.98.60"
CN0575_IP = "192.168.10.2"
SWIOT_IP = "192.168.97.40"
TCP_PORT = 10000
TIMEOUT = 5.0
AUTO_REFRESH_MS = 5000
GRAPH_MAX_POINTS = 60
DC_RPM = 4500
PWM_PERIOD = 0.01


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


class BoardPanel(ttk.LabelFrame):
    """UI panel for a single APARD board with LED control and temperature."""

    def __init__(self, parent, board_name, ip, log_callback):
        super().__init__(parent, text=f"  {board_name} ({ip})  ", padding=10)
        self.board_name = board_name
        self.ip = ip
        self.log = log_callback
        self._build_ui()

    def _build_ui(self):
        # -- Status --
        status_frame = ttk.Frame(self)
        status_frame.pack(fill="x", pady=(0, 8))

        ttk.Label(status_frame, text="Status:").pack(side="left")
        self.status_label = ttk.Label(
            status_frame, text="Unknown", foreground="gray",
            font=("TkDefaultFont", 10, "bold")
        )
        self.status_label.pack(side="left", padx=6)
        ttk.Button(
            status_frame, text="Test", command=self._test_connection
        ).pack(side="right")

        # -- LED control --
        led_frame = ttk.LabelFrame(self, text="LED Control", padding=6)
        led_frame.pack(fill="x", pady=4)

        btn_row = ttk.Frame(led_frame)
        btn_row.pack(fill="x")

        ttk.Button(
            btn_row, text="LED ON", command=lambda: self._send("LED_ON")
        ).pack(side="left", padx=2, expand=True, fill="x")

        ttk.Button(
            btn_row, text="LED OFF", command=lambda: self._send("LED_OFF")
        ).pack(side="left", padx=2, expand=True, fill="x")

        ttk.Button(
            btn_row, text="Status", command=lambda: self._send("LED_STATUS")
        ).pack(side="left", padx=2, expand=True, fill="x")

        self.led_state_label = ttk.Label(
            led_frame, text="LED State: --",
            font=("TkDefaultFont", 10)
        )
        self.led_state_label.pack(pady=(6, 0))

    def _test_connection(self):
        def worker():
            resp = send_command(self.ip, "LED_STATUS")
            self.after(0, lambda: self._on_test_result(resp))
        threading.Thread(target=worker, daemon=True).start()

    def _on_test_result(self, resp):
        if resp is not None:
            self.status_label.config(text="Reachable", foreground="green")
            self.log(self.board_name, f"Board reachable at {self.ip}")
            if resp.startswith("LED:"):
                self.led_state_label.config(text=f"LED State: {resp[4:]}")
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

        if cmd in ("LED_ON", "LED_OFF") and resp == "OK":
            state = "ON" if cmd == "LED_ON" else "OFF"
            self.led_state_label.config(text=f"LED State: {state}")
        elif cmd == "LED_STATUS" and resp.startswith("LED:"):
            self.led_state_label.config(text=f"LED State: {resp[4:]}")

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
        # -- Status --
        status_frame = ttk.Frame(self)
        status_frame.pack(fill="x", pady=(0, 8))

        ttk.Label(status_frame, text="Status:").pack(side="left")
        self.status_label = ttk.Label(
            status_frame, text="Unknown", foreground="gray",
            font=("TkDefaultFont", 10, "bold")
        )
        self.status_label.pack(side="left", padx=6)
        ttk.Button(
            status_frame, text="Test", command=self._test_connection
        ).pack(side="right")

        # -- Current temperature --
        temp_value_frame = ttk.Frame(self)
        temp_value_frame.pack(fill="x", pady=4)

        self.temp_label = ttk.Label(
            temp_value_frame, text="ADT75 Temp: -- °C",
            font=("TkDefaultFont", 16, "bold")
        )
        self.temp_label.pack(side="left", padx=5)

        # -- Controls --
        ctrl_frame = ttk.Frame(self)
        ctrl_frame.pack(fill="x", pady=4)

        ttk.Button(
            ctrl_frame, text="Read Temp",
            command=self._read_temp
        ).pack(side="left", padx=2)

        ttk.Checkbutton(
            ctrl_frame, text="Live graph (5s refresh)",
            variable=self.auto_refresh_var,
            command=self._toggle_auto_refresh
        ).pack(side="left", padx=10)

        ttk.Button(
            ctrl_frame, text="Clear Graph",
            command=self._clear_graph
        ).pack(side="right", padx=2)

        # -- Graph --
        if HAS_MATPLOTLIB:
            self.fig = Figure(figsize=(5, 2.5), dpi=80)
            self.fig.patch.set_facecolor("#f0f0f0")
            self.ax = self.fig.add_subplot(111)
            self.ax.set_xlabel("Time (s)")
            self.ax.set_ylabel("Temp (°C)")
            self.ax.set_title("ADT75 Live Temperature", fontsize=10)
            self.ax.grid(True, alpha=0.3)
            self.line, = self.ax.plot([], [], "b-o", markersize=3, linewidth=1.5)
            self.fig.tight_layout()

            self.canvas = FigureCanvasTkAgg(self.fig, master=self)
            self.canvas.get_tk_widget().pack(fill="both", expand=True, pady=(8, 0))
        else:
            ttk.Label(
                self, text="Install matplotlib for live graph:\npip3 install matplotlib",
                foreground="gray"
            ).pack(fill="both", expand=True, pady=20)

    def _test_connection(self):
        def worker():
            resp = send_command(self.ip, "READ_TEMP")
            self.after(0, lambda: self._on_test_result(resp))
        threading.Thread(target=worker, daemon=True).start()

    def _on_test_result(self, resp):
        if resp is not None and resp.startswith("TEMP:"):
            self.status_label.config(text="Reachable", foreground="green")
            self.log(self.board_name, f"Sensor reachable — {resp}")
            self.temp_label.config(text=f"ADT75 Temp: {resp[5:]} °C")
        else:
            self.status_label.config(text="Unreachable", foreground="red")
            self.log(self.board_name, f"Cannot reach {self.ip}")

    def _read_temp(self):
        def worker():
            self.after(0, lambda: self.log(self.board_name, ">> READ_TEMP"))
            resp = send_command(self.ip, "READ_TEMP")
            if resp is None:
                self.after(0, lambda: self._on_error())
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

            self.temp_label.config(text=f"ADT75 Temp: {temp:.1f} °C")

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
            t_min = min(temps)
            t_max = max(temps)
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
            self.auto_refresh_job = self.after(
                AUTO_REFRESH_MS, self._auto_refresh_tick
            )

    def cleanup(self):
        self.auto_refresh_var.set(False)
        if self.auto_refresh_job:
            self.after_cancel(self.auto_refresh_job)


class SWIOT1LPanel(ttk.LabelFrame):
    """UI panel for SWIOT1L fan PWM control and speed graph."""

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
        ttk.Button(
            status_frame, text="Connect", command=self._connect
        ).pack(side="right")

        ctrl_frame = ttk.LabelFrame(self, text="PWM Control", padding=6)
        ctrl_frame.pack(fill="x", pady=4)

        dc_row = ttk.Frame(ctrl_frame)
        dc_row.pack(fill="x")

        ttk.Label(dc_row, text="Duty Cycle (%):").pack(side="left")
        self.dc_entry = ttk.Entry(dc_row, width=8)
        self.dc_entry.insert(0, "0")
        self.dc_entry.pack(side="left", padx=4)

        ttk.Button(
            dc_row, text="Set PWM", command=self._set_pwm
        ).pack(side="left", padx=2)

        ttk.Button(
            dc_row, text="Stop", command=self._stop_pwm
        ).pack(side="left", padx=2)

        self.dc_label = ttk.Label(
            ctrl_frame, text="Current: 0% — 0 RPM",
            font=("TkDefaultFont", 10)
        )
        self.dc_label.pack(pady=(6, 0))

        if HAS_MATPLOTLIB:
            self.fig = Figure(figsize=(5, 2.5), dpi=80)
            self.fig.patch.set_facecolor("#f0f0f0")
            self.ax = self.fig.add_subplot(111)
            self.ax.set_xlabel("Time (s)")
            self.ax.set_ylabel("Speed (RPM)")
            self.ax.set_title("Fan Speed", fontsize=10)
            self.ax.grid(True, alpha=0.3)
            self.line, = self.ax.plot([], [], "r-o", markersize=3, linewidth=1.5)
            self.fig.tight_layout()

            self.canvas = FigureCanvasTkAgg(self.fig, master=self)
            self.canvas.get_tk_widget().pack(fill="both", expand=True, pady=(8, 0))
        else:
            ttk.Label(
                self, text="Install matplotlib for fan speed graph:\npip3 install matplotlib",
                foreground="gray"
            ).pack(fill="both", expand=True, pady=20)

    def _connect(self):
        if not HAS_ADI:
            self.log(self.board_name, "pyadi-iio not installed — pip3 install pyadi-iio")
            self.status_label.config(text="No pyadi-iio", foreground="red")
            return

        self.status_label.config(text="Connecting...", foreground="orange")
        self.log(self.board_name, "Connecting to SWIOT1L...")

        def worker():
            try:
                self.after(0, lambda: self.log(self.board_name, "Resetting to config mode..."))
                swiot = adi.swiot(uri=f"ip:{SWIOT_IP}")
                swiot.mode = "config"
                swiot = adi.swiot(uri=f"ip:{SWIOT_IP}")

                self.after(0, lambda: self.log(self.board_name, "Configuring channels..."))
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

                self.after(0, lambda: self.log(self.board_name, "Switching to runtime mode..."))
                swiot.mode = "runtime"

                self.after(0, lambda: self.log(self.board_name, "Initializing MAX14906..."))
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
        self.log(self.board_name, "Connected — ready for PWM control")

    def _on_connect_error(self, err):
        self.status_label.config(text="Error", foreground="red")
        self.log(self.board_name, f"Connection failed: {err}")

    def _set_pwm(self):
        if not self.connected:
            self.log(self.board_name, "Not connected — click Connect first")
            return

        try:
            dc = float(self.dc_entry.get())
            dc = max(0.0, min(100.0, dc))
        except ValueError:
            self.log(self.board_name, "Invalid duty cycle value")
            return

        self.duty_cycle = dc
        rpm = DC_RPM * dc / 100.0
        self.dc_label.config(text=f"Current: {dc:.0f}% — {rpm:.0f} RPM")
        self.log(self.board_name, f"Duty cycle set to {dc:.0f}%")

        if not self.pwm_running:
            self.pwm_running = True
            threading.Thread(target=self._pwm_loop, daemon=True).start()
            self._graph_tick()

    def _stop_pwm(self):
        self.pwm_running = False
        self.duty_cycle = 0.0
        self.dc_label.config(text="Current: 0% — 0 RPM")
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
            s_min = min(speeds)
            s_max = max(speeds)
            margin = max(100, (s_max - s_min) * 0.2)
            self.ax.set_ylim(max(0, s_min - margin), s_max + margin)
        else:
            self.ax.set_ylim(0, DC_RPM + 500)

        self.canvas.draw_idle()

    def _clear_graph(self):
        self.speed_history.clear()
        self.time_history.clear()
        self.start_time = None
        if HAS_MATPLOTLIB:
            self.line.set_data([], [])
            self.ax.set_xlim(0, 10)
            self.ax.set_ylim(0, DC_RPM + 500)
            self.canvas.draw_idle()

    def cleanup(self):
        self.pwm_running = False
        if self.connected and self.max14906:
            try:
                self.max14906.channel["voltage0"].raw = 0
            except Exception:
                pass


class ControlPanel(tk.Tk):
    """Main application window."""

    def __init__(self):
        super().__init__()
        self.title("Industrial Demo — Control Panel")
        self.geometry("1100x900")
        self.minsize(900, 700)

        # Top row: APARD boards side by side (no graphs, compact)
        top_frame = ttk.Frame(self)
        top_frame.pack(fill="x", padx=10, pady=5)

        self.board1 = BoardPanel(
            top_frame, "APARD #1", APARD1_IP, self._log_message
        )
        self.board1.pack(side="left", fill="both", expand=True, padx=(0, 5))

        self.board2 = BoardPanel(
            top_frame, "APARD #2", APARD2_IP, self._log_message
        )
        self.board2.pack(side="left", fill="both", expand=True, padx=(5, 0))

        # Bottom row: CN0575 + SWIOT1L (both with graphs)
        bottom_frame = ttk.Frame(self)
        bottom_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.cn0575 = CN0575Panel(bottom_frame, self._log_message)
        self.cn0575.pack(side="left", fill="both", expand=True, padx=(0, 5))

        self.swiot = SWIOT1LPanel(bottom_frame, self._log_message)
        self.swiot.pack(side="left", fill="both", expand=True, padx=(5, 0))

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

        ttk.Button(
            btn_frame, text="Test All", command=self._test_all
        ).pack(side="left", padx=5)

        ttk.Button(
            btn_frame, text="Clear Log", command=self._clear_log
        ).pack(side="left", padx=5)

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
        self.cn0575.cleanup()
        self.swiot.cleanup()
        self.destroy()


if __name__ == "__main__":
    app = ControlPanel()
    app.mainloop()

