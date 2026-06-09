#!/usr/bin/env python3
"""
adxl355_server.py — runs on the Raspberry Pi with the ADXL355 connected.

Streams accelerometer data (m/s²) over TCP to the monitoring GUI.

Usage:
    python adxl355_server.py [--rate 1000] [--chunk 256] [--port 50055] [--demo]
"""
import argparse
import json
import socket
import struct
import numpy as np

parser = argparse.ArgumentParser()
parser.add_argument("--rate",  type=int, default=1000,  help="Sample rate (Hz)")
parser.add_argument("--chunk", type=int, default=256,   help="Samples per frame")
parser.add_argument("--port",  type=int, default=50055, help="TCP port to listen on")
parser.add_argument("--demo",  action="store_true",     help="Synthetic data (no hardware)")
args = parser.parse_args()

FS    = args.rate
CHUNK = args.chunk
PORT  = args.port

# ---------------------------------------------------------------------------
# Demo source (no hardware)
# ---------------------------------------------------------------------------
class DemoSource:
    def __init__(self):
        self._t = 0.0
        self._fault_timer = 0

    def read_chunk(self, n):
        t = np.linspace(self._t, self._t + n / FS, n, endpoint=False)
        self._t += n / FS
        data = []
        for i in range(3):
            freq = [50, 100, 150][i]
            amp  = [0.05, 0.03, 0.02][i]
            sig  = amp * np.sin(2 * np.pi * freq * t) + 0.01 * np.random.randn(n)
            if i == 0:
                self._fault_timer += n
                if self._fault_timer > FS * 8:
                    self._fault_timer = 0
                    idx = np.random.randint(0, n)
                    sig[idx] += 0.8 * np.random.choice([-1, 1])
            data.append(sig * 9.80665)   # m/s²
        return data

# ---------------------------------------------------------------------------
# Hardware source
# ---------------------------------------------------------------------------
def make_source():
    if args.demo:
        print("DEMO mode — no hardware required")
        return DemoSource()

    try:
        import adi
        print("Connecting to ADXL355...")
        dev = adi.adxl355()
        try:
            dev.sample_rate = FS
            print(f"Sample rate set: {int(dev.sample_rate)} Hz")
        except AttributeError:
            print(f"Sample rate fixed at {FS} Hz")
        dev.rx_buffer_size = CHUNK

        class _Dev:
            def read_chunk(self, _n):
                return dev.rx()   # returns list of 3 arrays in m/s²

        return _Dev()
    except Exception as exc:
        print(f"Could not connect to ADXL355: {exc}")
        print("Tip: run with --demo to test without hardware")
        raise SystemExit(1)

# ---------------------------------------------------------------------------
# TCP server helpers
# ---------------------------------------------------------------------------
def _send_msg(conn: socket.socket, payload: bytes) -> None:
    """Length-prefix a message and send it."""
    conn.sendall(struct.pack('>I', len(payload)) + payload)

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    source = make_source()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', PORT))
    srv.listen(1)
    print(f"Listening on 0.0.0.0:{PORT}  |  FS={FS} Hz  |  chunk={CHUNK}")

    while True:
        conn, addr = srv.accept()
        print(f"Client connected: {addr}")
        try:
            # Handshake: send server config so the client can validate settings
            cfg = json.dumps({"fs": FS, "chunk": CHUNK}).encode()
            _send_msg(conn, cfg)

            while True:
                raw = source.read_chunk(CHUNK)         # list of 3 arrays, m/s²
                arr = np.array(raw, dtype=np.float32)  # shape (3, N)
                _send_msg(conn, arr.tobytes())

        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            print(f"Client disconnected: {exc}")
        finally:
            conn.close()


if __name__ == "__main__":
    main()