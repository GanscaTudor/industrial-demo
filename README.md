# Industrial Demo — 10BASE-T1L Control Network

Industrial control network demo using Analog Devices 10BASE-T1L Single Pair Ethernet. 

## Hardware

| Board | Description | IP Address |
|-------|------------|------------|
| Raspberry Pi 4 + [EVAL-T1LPSE](https://www.analog.com/en/resources/evaluation-hardware-and-software/evaluation-boards-kits/eval-t1lpse.html) | Main controller — runs the GUI, acts as TCP client and SPoE PSE | 192.168.98.1 |
| [AD-APARD32690-SL](https://www.analog.com/en/resources/evaluation-hardware-and-software/evaluation-boards-kits/ad-apard32690-sl.html) #1 + [AD-APARDPFWD-SL](https://www.analog.com/en/resources/evaluation-hardware-and-software/evaluation-boards-kits/ad-apardpfwd-sl.html) | APARD #1 — MAX32690 MCU with ADIN2111 (dual-port T1L MAC-PHY) | 192.168.98.50 |
| [AD-APARD32690-SL](https://www.analog.com/en/resources/evaluation-hardware-and-software/evaluation-boards-kits/ad-apard32690-sl.html) #2 + [AD-APARDSPOE-SL](https://www.analog.com/en/resources/evaluation-hardware-and-software/evaluation-boards-kits/ad-apardspoe-sl.html) | APARD #2 — MAX32690 MCU with ADIN1110 (single-port T1L MAC-PHY) | 192.168.98.60 |
| Raspberry Pi 4 + [EVAL-CN0575-RPIZ](https://www.analog.com/en/resources/evaluation-hardware-and-software/evaluation-boards-kits/eval-cn0575-rpiz.html) | CN0575 — ADT75 temperature sensor over T1L | 192.168.10.2 |
| [AD-T1LUSB-EBZ](https://wiki.analog.com/resources/eval/user-guides/ad-t1lusb-ebz) | USB-to-T1L adapter — plugged into a USB port on the main RPi | — |
| [EVAL-AD-SWIOT1L-SL](https://www.analog.com/en/resources/evaluation-hardware-and-software/evaluation-boards-kits/eval-ad-swiot1l-sl.html) | SWIOT1L — MAX14906 digital output + AD74413R analog I/O (independently powered) | 192.168.97.40 |

### Additional Components

- 2x LED + 330 ohm resistor (wired to P2.7 / GPIO_2 on each APARD board header P7)
- DC fan connected to SWIOT1L MAX14906 channel 0 (digital output for PWM)
- Single Pair Ethernet cables (T1L) between all boards
- USB cables for flashing APARD boards via DAPLINK

## Software and Firmware

### Operating Systems

| Target | OS / Framework |
|--------|---------------|
| Main RPi (T1LPSE) | [ADI Kuiper Linux 2.0](https://wiki.analog.com/resources/tools-software/linux-software/kuiper-linux) |
| CN0575 RPi | [ADI Kuiper Linux 2.0](https://wiki.analog.com/resources/tools-software/linux-software/kuiper-linux) |
| APARD #1 and #2 | [no-OS](https://github.com/analogdevicesinc/no-OS) bare-metal C (MAX32690) |
| SWIOT1L | [no-OS](https://github.com/analogdevicesinc/no-OS) bare-metal C (MAX32670) |

### Python Dependencies (Main RPi)

```
matplotlib
pyadi-iio
```

## Network Topology

```
                           ┌───────────────────────┐
                           │  Main RPi + T1LPSE    │
                           │  192.168.98.1         │
                           │  (PSE + GUI client)   │
                           └──┬────────┬────────┬──┘
                    T1L       │        │        │       USB (AD-T1LUSB-EBZ)
                              │        │        │
                              v        │        v
                ┌──────────────────┐   │   ┌──────────────────┐
                │  CN0575 + RPi    │   │   │     SWIOT1L      │
                │  192.168.10.2    │   │   │  192.168.97.40   │
                │  ADT75 temp      │   │   │  (own power)     │
                └──────────────────┘   │   └──────────────────┘
                                       │
                                  T1L  │
                                       v
                            ┌──────────────────┐
                            │  APARD #1 (PFWD) │
                            │  192.168.98.50   │
                            │  ADIN2111        │
                            └────────┬─────────┘
                                     │
                                T1L  │  (daisy chain)
                                     v
                            ┌──────────────────┐
                            │  APARD #2 (SPOE) │
                            │  192.168.98.60   │
                            │  ADIN1110        │
                            └──────────────────┘
```

## Repository Structure

```
industrial-demo/
├── RPI_T1LPSE/
│   └── main_app.py              # Tkinter GUI — runs on main RPi
├── RPI_CN0575/
│   └── cn0575_state_machine.py  # TCP server — runs on CN0575 RPi
├── firmware_changes/            # C firmware modifications for APARD boards
│   ├── apard_communication_example.c
│   ├── common_data_apard1.c
│   ├── common_data_apard2.c
│   └── common_data.h
└── README.md
```

## Command Protocol (APARD Boards)

Text-based, newline-terminated, over TCP port 10000. One connection per command.

| Command | Response | Description |
|---------|----------|-------------|
| `LED_ON\n` | `OK\n` | Set LED GPIO high |
| `LED_OFF\n` | `OK\n` | Set LED GPIO low |
| `LED_STATUS\n` | `LED:ON\n` or `LED:OFF\n` | Read current LED state |
| `READ_TEMP\n` | `TEMP:25.1\n` | Read MAX32690 die temperature |

## Command Protocol (CN0575)

| Command | Response | Description |
|---------|----------|-------------|
| `READ_TEMP\n` | `TEMP:24.3\n` | Read ADT75 temperature sensor |

## SWIOT1L Control

The SWIOT1L is controlled via [pyadi-iio](https://github.com/analogdevicesinc/pyadi-iio) (not TCP). The GUI connects directly using `adi.swiot()` and `adi.max14906()` to set PWM duty cycle on the MAX14906 digital output driving the fan.

## Quick Start

### 1. CN0575 RPi — Start the temperature server

```bash
ssh analog@192.168.10.2
python3 /home/analog/cn0575_state_machine.py
```

To run at boot as a systemd service:

```bash
sudo systemctl enable cn0575-server
sudo systemctl start cn0575-server
```

### 2. APARD Boards — Flash the firmware

Flash `apard_communication_example.c` via DAPLINK using the no-OS build system:

```bash
cd no-OS/projects/apardpfwd
make RELEASE=y -j
# Connect DAPLINK USB and flash
```

### 3. Main RPi — Run the control panel

```bash
pip3 install matplotlib pyadi-iio
python3 RPI_T1LPSE/main_app.py
```

## GUI Overview

The control panel has four panels in a 2x2 layout:

- **APARD #1 / #2** (top row) — LED ON/OFF/Status buttons
- **CN0575** (bottom left) — ADT75 temperature with live graph
- **SWIOT1L** (bottom right) — Fan duty cycle input with speed graph

All TCP operations run in background threads to keep the GUI responsive. A communication log at the bottom shows all commands and responses.
