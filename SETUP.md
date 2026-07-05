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
| JBD BMS 8S (24V) | BLE → RS485 planned | A4:C1:37:54:73:05 | JST serial port available |
| JBD BMS 4S (12V) | BLE → RS485 planned | A5:C2:37:30:C9:EA | JST serial port available |
| Sunster diesel heater | BLE | DC:23:4F:ED:D7:D2 | App: com.clj.airheater (Hcalory/Vevor protocol) |
| PowMR HHJ60-PRO MPPT | RS485 Modbus RTU | — | SRNE-based, register map from SRNE docs |
| PowMR POW-HVM4.2K-24V-D | RS485 RJ45 direct (planned) | 192.168.88.238 (datalogger) | Cloud app: com.ssli.next.solar (Siseli/Solar of Things); cloud updates too slow (5min) |
| PZEM shunt (model TBD) | RS485 Modbus RTU | — | Not yet installed |

## Software Stack

| Service | Install path | Config |
|---------|-------------|--------|
| Mosquitto | apt | /etc/mosquitto/ |
| Home Assistant Core 2026.2.3 | /srv/homeassistant (venv) | /home/homeassistant/.homeassistant/ |
| BMS collector | /opt/bms_collector.py | Systemd: bms-collector.service |
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

### 3. BMS Collector

Install `/opt/bms_collector.py` (see repository).

```bash
pip3 install bleak paho-mqtt --break-system-packages
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

```bash
systemctl daemon-reload
systemctl enable --now bms-collector
systemctl enable --now homeassistant
```

### 4. HA Configuration

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

- [ ] Switch JBD BMS from BLE to RS485/UART (JST connector, 9600 baud, same JBD protocol)
- [ ] Wire PowMR inverter RS485 RJ45 port directly (bypass cloud datalogger)
- [ ] Wire MPPT HHJ60-PRO RS485 (SRNE Modbus RTU)
- [ ] Install and wire PZEM shunt
- [ ] Diesel heater BLE integration (Hcalory/Vevor protocol, bderleta/vevor-ble-bridge)
- [ ] Build HA dashboard
