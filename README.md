# Industrial Demo — 10BASE-T1L Control Network

Industrial control network demo using Analog Devices 10BASE-T1L Single Pair Ethernet. A Raspberry Pi running Kuiper Linux 2 acts as the central controller, communicating with multiple ADI evaluation boards over T1L to control LEDs, read temperatures, and drive a DC fan — all managed from a single Python GUI.

## Hardware

| Board | Description | IP Address |
|-------|------------|------------|
| Raspberry Pi 4 + [AD-RPI-T1LPSE-SL](https://www.analog.com/en/resources/evaluation-hardware-and-software/evaluation-boards-kits/ad-rpi-t1lpse-sl.html) | Main controller — runs the GUI, acts as TCP client and SPoE PSE | 192.168.98.1 |
| [AD-APARD32690-SL](https://www.analog.com/en/resources/evaluation-hardware-and-software/evaluation-boards-kits/ad-apard32690-sl.html) #1 + [AD-APARDPFWD-SL](https://www.analog.com/en/resources/evaluation-hardware-and-software/evaluation-boards-kits/ad-apardpfw-sl.html) | APARD #1 — MAX32690 MCU with ADIN2111 (dual-port T1L MAC-PHY) | 192.168.98.50 |
| [AD-APARD32690-SL](https://www.analog.com/en/resources/evaluation-hardware-and-software/evaluation-boards-kits/ad-apard32690-sl.html) #2 + [AD-APARDSPOE-SL](https://www.analog.com/en/resources/evaluation-hardware-and-software/evaluation-boards-kits/ad-apardspoe-sl.html) | APARD #2 — MAX32690 MCU with ADIN1110 (single-port T1L MAC-PHY) | 192.168.98.60 |
| Raspberry Pi 4 + [EVAL-CN0575-RPIZ](https://analogdevicesinc.github.io/documentation/solutions/reference-designs/eval-cn0575-rpiz/index.html) | CN0575 — ADT75 temperature sensor over T1L | 192.168.10.2 |
| [AD-T1LUSB-EBZ](https://analogdevicesinc.github.io/documentation/solutions/reference-designs/ad-apard32690-sl/ad-t1lusb-ebz/index.html) | USB-to-T1L adapter — plugged into a USB port on the main RPi | — |
| [EVAL-AD-SWIOT1L-SL](https://www.analog.com/en/resources/evaluation-hardware-and-software/evaluation-boards-kits/ad-swiot1l-sl.html) | SWIOT1L — MAX14906 digital output + AD74413R analog I/O (independently powered) | 192.168.97.40 |

### Additional Components

- 2x LED + 330 ohm resistor (wired to P2.7 / GPIO_2 on each APARD board header P7)
- DC fan connected to SWIOT1L MAX14906 channel 0 (digital output for PWM)
- Single Pair Ethernet cables (T1L) between Main RPi, APARD #1, APARD #2, and CN0575
- SWIOT1L connects to the main RPi via the AD-T1LUSB-EBZ USB-to-T1L adapter (not directly through the T1LPSE)
- USB cables for flashing APARD boards via DAPLINK
- MaxDAP Pico programmer for OpenOCD flashing

## Network Topology

<img src="images/Hardware topology.png" alt="Hardware Topology" width="800">

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

## Setup Guide

### Phase 1 — System Verification

Make sure both Raspberry Pis are running [ADI Kuiper Linux 2.0](https://wiki.analog.com/resources/tools-software/linux-software/kuiper-linux):

Verify the AD-RPI-T1LPSE hat is seated on the main RPi's 40-pin GPIO header. Then check that the T1L PSE device tree overlay is applied:

```bash
grep "dtoverlay=rpi-t1lpse-class12" /boot/config.txt
```

If the overlay is missing, add it:

```bash
echo "dtoverlay=rpi-t1lpse-class12" | sudo tee -a /boot/config.txt
sudo reboot
```
Make sure it was added under [Pi4] section if you are using a Raspberry Pi 4.
After reboot, confirm the overlay is loaded:

```bash
dtoverlay -l | grep t1lpse
```

### Phase 2 — Install Prerequisites

Install build tools, libraries, and SDKs on the main RPi.

#### Build Tools and ARM Toolchain

```bash
sudo apt update
sudo apt install -y git make gcc-arm-none-eabi libnewlib-arm-none-eabi
```

Verify the installation:

```bash
arm-none-eabi-gcc --version
make --version | head -1
git --version
```

#### MaximSDK (headers and libraries only)

```bash
git clone https://github.com/analogdevicesinc/msdk.git ~/MaximSDK
```

Create a GNUTools symlink so the no-OS build system finds the system compiler:

```bash
mkdir -p ~/MaximSDK/Tools/GNUTools/10.3/bin
ln -s /usr/bin/arm-none-eabi-* ~/MaximSDK/Tools/GNUTools/10.3/bin/
```

Add the environment variable to `~/.bashrc`:

```bash
echo 'export MAXIM_LIBRARIES=~/MaximSDK/Libraries' >> ~/.bashrc
source ~/.bashrc
```

#### no-OS

Clone the repository and patch the build files for missing source dependencies:

```bash
git clone --recursive https://github.com/analogdevicesinc/no-OS.git ~/no-OS
```

Append the required peripheral driver sources to the project build file:

```bash
cat >> ~/no-OS/projects/apardpfwd/src.mk << 'EOF'

SRCS += $(MAXIM_LIBRARIES)/PeriphDrivers/Source/SYS/mxc_delay.c \
        $(MAXIM_LIBRARIES)/PeriphDrivers/Source/SYS/mxc_lock.c

INCS += $(MAXIM_LIBRARIES)/PeriphDrivers/Include/MAX32690/mxc_delay.h \
        $(MAXIM_LIBRARIES)/PeriphDrivers/Include/MAX32690/mxc_lock.h
EOF
```

#### libiio (v0 branch)

```bash
sudo apt-get install -y libxml2 libxml2-dev bison flex libcdk5-dev cmake \
    libaio-dev libusb-1.0-0-dev libserialport-dev libavahi-client-dev

git clone https://github.com/analogdevicesinc/libiio.git --branch libiio-v0 ~/libiio
cd ~/libiio && mkdir build && cd build
cmake .. -DPYTHON_BINDINGS=ON
make -j && sudo make install
sudo ldconfig
```

#### pyadi-iio

```bash
sudo apt-get install -y python3 libatlas-base-dev
git clone https://github.com/analogdevicesinc/pyadi-iio ~/pyadi-iio
cd ~/pyadi-iio
sudo python3 -m pip install -r requirements_prod_test.txt
sudo pip install .
```

#### OpenOCD (ADI fork)

```bash
sudo apt-get install -y libtool pkg-config libusb-1.0-0-dev libhidapi-dev libgpiod-dev

mkdir -p ~/work && cd ~/work
git clone https://github.com/analogdevicesinc/openocd -b "0.12.0-1.1.2" --depth 1 --recurse-submodules
cd openocd
./bootstrap
./configure --enable-cmsis-dap --enable-linuxgpiod --disable-werror
make -j && sudo make install
```

#### Network Manager

```bash
sudo apt-get install -y network-manager
```

### Phase 3 — Network Configuration

The demo uses multiple network interfaces and subnets. Configure each on the main RPi.

#### T1LPSE Interface (APARD subnet — 192.168.98.x)

The T1LPSE hat creates an Ethernet interface for the 10BASE-T1L network. Assign a static IP for the APARD boards:

```bash
sudo nmcli connection add type ethernet con-name t1l-apard \
    ifname <t1l-interface> \
    ipv4.addresses 192.168.98.1/24 \
    ipv4.method manual
sudo nmcli connection up t1l-apard
```

#### CN0575 Interface (CN0575 subnet — 192.168.10.x)

The CN0575 RPi is on a separate subnet, reached through a different T1L port on the T1LPSE:

```bash
sudo nmcli connection add type ethernet con-name t1l-cn0575 \
    ifname <cn0575-t1l-interface> \
    ipv4.addresses 192.168.10.1/24 \
    ipv4.method manual
sudo nmcli connection up t1l-cn0575
```

#### USB-T1L Interface (SWIOT1L subnet)

The AD-T1LUSB-EBZ adapter creates a separate Ethernet interface for the SWIOT1L:

```bash
sudo nmcli connection add type ethernet con-name t1l-swiot \
    ifname <usb-t1l-interface> \
    ipv4.addresses 192.168.97.1/24 \
    ipv4.method manual
sudo nmcli connection up t1l-swiot
```

#### Verify Connectivity

Once all boards are powered and the network is configured, verify connectivity:

```bash
ping -c 3 192.168.98.50   # APARD #1
ping -c 3 192.168.98.60   # APARD #2
ping -c 3 192.168.10.2    # CN0575
ping -c 3 192.168.97.40   # SWIOT1L
```

### Phase 4 — Build Firmware

#### APARD #1 (ADIN2111, IP 192.168.98.50)

```bash
cd ~/no-OS/projects/apardpfwd

sed -i 's/^NO_OS_IP=.*/NO_OS_IP=192.168.98.50/' Makefile
sed -i 's/^NO_OS_NETMASK=.*/NO_OS_NETMASK=255.255.0.0/' Makefile
sed -i 's/^NO_OS_GATEWAY=.*/NO_OS_GATEWAY=0.0.0.0/' Makefile

make clean && make RELEASE=y -j
cp build/apardpfwd.elf /home/analog/apard1.elf
```

The default `common_data.c` configuration is used for APARD #1:

| Parameter | Value |
|-----------|-------|
| `adin1110_rst_gpio_ip.port` | 2 |
| `adin1110_rst_gpio_ip.number` | 31 |
| `adin1110_spi_ip.device_id` | 0 |
| `adin1110_ip.chip_type` | ADIN2111 |

#### APARD #2 (ADIN1110, IP 192.168.98.60)

Modify `src/common/common_data.c` for the second board's hardware connections, then build:

```bash
cd ~/no-OS/projects/apardpfwd

COMMON_DATA="src/common/common_data.c"
sed -i 's/\.port = 2,/\.port = 1,/' $COMMON_DATA
sed -i 's/\.number = 31,/\.number = 5,/' $COMMON_DATA
sed -i 's/\.device_id = 0,/\.device_id = 4,/' $COMMON_DATA
sed -i 's/\.chip_type = ADIN2111,/\.chip_type = ADIN1110,/' $COMMON_DATA

sed -i 's/^NO_OS_IP=.*/NO_OS_IP=192.168.98.60/' Makefile

make clean && make RELEASE=y -j
cp build/apardpfwd.elf /home/analog/apard2.elf
```

| Parameter | Value |
|-----------|-------|
| `adin1110_rst_gpio_ip.port` | 1 |
| `adin1110_rst_gpio_ip.number` | 5 |
| `adin1110_spi_ip.device_id` | 4 |
| `adin1110_ip.chip_type` | ADIN1110 |

Restore `common_data.c` back to APARD #1 defaults after building:

```bash
sed -i 's/\.port = 1,/\.port = 2,/' $COMMON_DATA
sed -i 's/\.number = 5,/\.number = 31,/' $COMMON_DATA
sed -i 's/\.device_id = 4,/\.device_id = 0,/' $COMMON_DATA
sed -i 's/\.chip_type = ADIN1110,/\.chip_type = ADIN2111,/' $COMMON_DATA
```

#### SWIOT1L Firmware

Download the pre-built SWIOT1L static IP firmware from the official release:

```bash
wget -O /home/analog/swiot1l_static_ip.hex \
    https://github.com/analogdevicesinc/no-OS/releases/download/swiot1l-v1.1.0/swiot1l_maxim_swiot1l_static_ip.hex
```

### Phase 5 — Flash Firmware

All boards are flashed using OpenOCD with a MaxDAP Pico (CMSIS-DAP) programmer.

#### Flash APARD #1

Connect the MaxDAP Pico to APARD #1, then run:

```bash
openocd -f interface/cmsis-dap.cfg -f target/max32690.cfg \
    -c "program /home/analog/apard1.elf verify reset exit"
```

#### Flash APARD #2

Move the MaxDAP Pico to APARD #2, then run:

```bash
openocd -f interface/cmsis-dap.cfg -f target/max32690.cfg \
    -c "program /home/analog/apard2.elf verify reset exit"
```

#### Flash SWIOT1L

Connect the DAPLink to the SWIOT1L board, then run:

```bash
openocd -f interface/cmsis-dap.cfg -f target/max32690.cfg \
    -c "program /home/analog/swiot1l_static_ip.hex verify reset exit"
```

### Phase 6 — Start the Demo

#### 1. Start the CN0575 temperature server

On the CN0575 Raspberry Pi:

```bash
ssh analog@192.168.10.2
python3 /home/analog/cn0575_state_machine.py
```

This starts a TCP server on port 10000 that reads the ADT75 temperature sensor via IIO and responds to `READ_TEMP` commands.

#### 2. Run the control panel

On the main RPi:

```bash
pip3 install matplotlib pyadi-iio
python3 RPI_T1LPSE/main_app.py
```

The GUI provides:

- **APARD #1 and #2** — LED on/off control, LED status readback, and die temperature reading over TCP
- **CN0575** — Live ADT75 temperature graph with auto-refresh
- **SWIOT1L** — Fan PWM duty cycle control with live RPM graph (via pyadi-iio)

## Communication Protocols

### APARD Boards (TCP port 10000)

Text-based, newline-terminated. One TCP connection per command.

| Command | Response | Description |
|---------|----------|-------------|
| `LED_ON\n` | `OK\n` | Set LED GPIO high |
| `LED_OFF\n` | `OK\n` | Set LED GPIO low |
| `LED_STATUS\n` | `LED:ON\n` or `LED:OFF\n` | Read current LED state |
| `READ_TEMP\n` | `TEMP:25.1\n` | Read MAX32690 die temperature |

### CN0575 (TCP port 10000)

| Command | Response | Description |
|---------|----------|-------------|
| `READ_TEMP\n` | `TEMP:24.3\n` | Read ADT75 temperature sensor |

### SWIOT1L (pyadi-iio)

The SWIOT1L is controlled via [pyadi-iio](https://github.com/analogdevicesinc/pyadi-iio) (not TCP). The GUI connects directly using `adi.swiot()` and `adi.max14906()` to set PWM duty cycle on the MAX14906 digital output driving the fan.

## Agentic AI Setup Assistant

<img src="images/Agentic AI.png" alt="Agentic AI Setup Assistant" width="800">

This demo includes a Claude Code agent with all setup steps embedded as skills. The agent can walk through the entire setup process interactively — from verifying the Kuiper system and installing prerequisites, to building and flashing firmware, configuring the network, and launching the GUI. It is designed to run directly on the main Raspberry Pi and handles each phase with user confirmation at critical steps (such as moving the programmer between boards).

> **Note:** The agent is still under development and not yet included in this repository.
