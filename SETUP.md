# Caravan Monitoring Setup

## Hardware

| Component | Details |
|-----------|---------|
| Compute | Raspberry Pi 3B v1.2, Debian 13 (trixie) arm64, 192.168.88.235 |
| Connectivity | Ethernet + WireGuard tunnel to home router |
| Bluetooth | Built-in RPi BT 4.1 (UART, not USB) |
| RS485 | USB-to-RS485 adapter (to be connected) |

## Devices

| Device | Protocol | ID / Address | Notes |
|--------|----------|-------------|-------|
| JBD BMS 8S (24V) | UART serial (CH340, /dev/ttyUSB0) | A4:C1:37:54:73:05 | Model OH20SA01L20S200A, SW 2.1. JST connector wired to CH340 USB-UART. RPi placed next to battery box, powered via GPIO 5V from 12V→5V step-down. |
| JBD BMS 4S (12V) | BLE → RS485 planned | A5:C2:37:30:C9:EA | JST serial port available |
| Sunster diesel heater | BLE | DC:23:4F:ED:D7:D2 | Tuya BLE protocol (service 0x1910, chars 0x2b10/0x2b11, device name "COMMON"). Not the older Hcalory FFE0 protocol. Needs local key — plan: sniff with Motorola phone HCI log. |
| PowMR HHJ60-PRO MPPT | RS485 (parallel sync only) | /dev/ttyUSB0 | RS485 is for multi-device sync only — broadcasts 15-byte identity frame, no sensor data. Need second unit to sniff inter-device protocol. Considering dual MPPT setup (also solves panel shading problem). |
| PowMR POW-HVM4.2K-24V-D | RS485 RJ45 direct (planned) | 192.168.88.238 (datalogger) | Cloud app: com.ssli.next.solar (Siseli/Solar of Things); cloud updates too slow (5min) |
| PZEM-017 shunt | RS485 Modbus RTU | /dev/ttyUSB1 (CH340) | FC04, 9600 baud, 2 stop bits, slave 1. Powered via USB. Not yet wired to actual shunt. |

## Software Stack

| Service | Install path | Config |
|---------|-------------|--------|
| Mosquitto | apt | /etc/mosquitto/ |
| Home Assistant Core 2026.2.3 | /srv/homeassistant (venv) | /home/homeassistant/.homeassistant/ |
| BMS collector | /opt/bms_collector.py | Systemd: bms-collector.service. Supports both UART serial (pyserial) and BLE per device. |
| PZEM collector | /opt/pzem_collector.py | Systemd: pzem-collector.service. PZEM-017 via Modbus RTU FC04, 9600 baud, 2 stop bits, slave 1. |
| HACS | HA custom_components | Via HA UI |
| Solar of Things | /home/homeassistant/.homeassistant/custom_components/solar_of_things/ | Via HA UI |

## Installation Steps

### 1. System prep

```bash
# Unblock Bluetooth (was rfkill soft-blocked on fresh Debian)
rfkill unblock bluetooth
systemctl restart bluetooth
bluetoothctl power on

# Dependencies
apt-get install -y python3 python3-dev python3-venv python3-pip bluez \
  libffi-dev libssl-dev libjpeg-dev zlib1g-dev autoconf build-essential \
  libopenjp2-7 libtiff-dev libturbojpeg0-dev tzdata ffmpeg \
  mosquitto mosquitto-clients
```

### 2. Home Assistant Core

```bash
useradd -rm homeassistant -G bluetooth
mkdir -p /srv/homeassistant
chown homeassistant:homeassistant /srv/homeassistant
sudo -u homeassistant python3 -m venv /srv/homeassistant
sudo -u homeassistant /srv/homeassistant/bin/pip install --no-cache-dir homeassistant
```

`/etc/systemd/system/homeassistant.service`:
```ini
[Unit]
Description=Home Assistant
After=network-online.target mosquitto.service
Wants=network-online.target

[Service]
Type=simple
User=homeassistant
WorkingDirectory=/home/homeassistant
ExecStart=/srv/homeassistant/bin/hass -c /home/homeassistant/.homeassistant
Restart=on-failure
RestartSec=5
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW

[Install]
WantedBy=multi-user.target
```

### 3. USB Serial Device Naming

Create `/etc/udev/rules.d/99-caravan-serial.rules` to assign stable device names
regardless of USB enumeration order. Port paths are fixed to physical USB ports on the RPi.

```
# 8S BMS - CH340 on USB port 1.2
SUBSYSTEM=="tty", ENV{ID_PATH}=="platform-3f980000.usb-usb-0:1.2:1.0", SYMLINK+="ttyBMS8S"

# PZEM-017 shunt - CH340 on USB port 1.4
SUBSYSTEM=="tty", ENV{ID_PATH}=="platform-3f980000.usb-usb-0:1.4:1.0", SYMLINK+="ttyPZEM"
```

```bash
udevadm control --reload-rules && udevadm trigger
# Verify:
ls -la /dev/ttyBMS8S /dev/ttyPZEM
```

If you add more devices or plug into different ports, check the path with:
```bash
udevadm info /dev/ttyUSBx | grep ID_PATH
```

### 5. BMS Collector

Install `/opt/bms_collector.py` (see repository).

```bash
pip3 install bleak paho-mqtt pyserial pymodbus --break-system-packages
```

`/etc/systemd/system/bms-collector.service`:
```ini
[Unit]
Description=JBD BMS MQTT Collector
After=network.target bluetooth.target mosquitto.service
Requires=mosquitto.service

[Service]
ExecStart=/usr/bin/python3 /opt/bms_collector.py
Restart=on-failure
RestartSec=10
User=root

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/pzem-collector.service`:
```ini
[Unit]
Description=PZEM-017 DC Shunt MQTT Collector
After=network.target mosquitto.service
Requires=mosquitto.service

[Service]
ExecStart=/usr/bin/python3 /opt/pzem_collector.py
Restart=on-failure
RestartSec=10
User=root

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now bms-collector
systemctl enable --now pzem-collector
systemctl enable --now homeassistant
```

### 6. HA Configuration

- Add **MQTT** integration: host `localhost`, port `1883`, no auth
- BMS sensors appear automatically via MQTT discovery
- **Disable HA Bluetooth integration** — it conflicts with bms-collector
- Install **HACS**: `cd /home/homeassistant/.homeassistant && wget -O - https://get.hacs.xyz | bash -`
- Install **Solar of Things** custom component (from github.com/Conexo-Casa/solar-of-things-ha)
  - Station ID: `495134682935951361`
  - Device ID: `495134683124695040`

## Known Issues & Fixes

### BLE gets stuck (InProgress / not found)

```bash
systemctl stop bms-collector
systemctl restart bluetooth
sleep 3
systemctl start bms-collector
```

Cause: HA Bluetooth integration re-enables on restart and grabs the adapter.
Fix: disable HA Bluetooth integration after every HA restart.

### HA Bluetooth integration re-enables after restart

Manual step: Settings → Devices & Services → Bluetooth → Disable.
TODO: find a way to permanently disable.

## Planned Work

- [x] Switch JBD BMS 8S from BLE to UART serial (JST → CH340 → /dev/ttyUSB0)
- [ ] Switch JBD BMS 4S from BLE to UART serial (needs second CH340 adapter)
- [ ] Wire PowMR inverter RS485 RJ45 port directly (bypass cloud datalogger)
- [ ] MPPT monitoring — requires second HHJ60-PRO unit to enable inter-device RS485 sniffing (also needed for dual-string setup to address panel shading)
- [ ] Wire PZEM-017 to actual shunt (currently reads 0V/0A)
- [ ] Diesel heater BLE integration — Tuya BLE protocol confirmed (service 0x1910); need BLE HCI snoop log from Motorola phone to extract local key. Samsung Galaxy S25 bugreport does not include BT log. Alternatively try `tuya_ble` HA custom component which handles key exchange automatically.
- [ ] Build HA dashboard
