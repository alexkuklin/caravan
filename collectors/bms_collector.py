#!/usr/bin/env python3
"""JBD BMS Bluetooth collector with Home Assistant MQTT auto-discovery."""

import asyncio
import json
import logging
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import paho.mqtt.client as mqtt
import serial as pyserial
from bleak import BleakClient, BleakError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bms")

MQTT_HOST           = "localhost"
MQTT_PORT           = 1883
POLL_INTERVAL_SERIAL = 2   # seconds between serial polls
POLL_INTERVAL_BLE    = 10  # seconds between BLE polls
CONNECT_TIMEOUT      = 2

NOTIFY_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
WRITE_UUID  = "0000ff02-0000-1000-8000-00805f9b34fb"
CMD_BASIC   = bytes([0xdd, 0xa5, 0x03, 0x00, 0xff, 0xfd, 0x77])
CMD_CELLS   = bytes([0xdd, 0xa5, 0x04, 0x00, 0xff, 0xfc, 0x77])

DEVICES = [
    {"serial_port": "/dev/ttyBMS8S", "name": "8s_battery", "label": "8S Battery"},
    {"mac": "A5:C2:37:30:C9:EA",   "name": "4s_battery", "label": "4S Battery"},
]


@dataclass
class BmsData:
    voltage: float
    current: float
    remaining_ah: float
    nominal_ah: float
    soc: int
    num_cells: int
    temps: list[float]
    cell_voltages: list[float]


def parse_basic(data: bytes) -> Optional[BmsData]:
    if len(data) < 28 or data[0] != 0xdd:
        return None
    voltage    = struct.unpack_from(">H", data, 4)[0] / 100
    current    = struct.unpack_from(">h", data, 6)[0] / 100
    remaining  = struct.unpack_from(">H", data, 8)[0] / 100
    nominal    = struct.unpack_from(">H", data, 10)[0] / 100
    soc        = data[23]
    num_cells  = data[25]
    num_temps  = data[26]
    temps      = [struct.unpack_from(">H", data, 27 + i * 2)[0] / 10 - 273.1
                  for i in range(num_temps)]
    return BmsData(voltage, current, remaining, nominal, soc, num_cells, temps, [])


def parse_cells(data: bytes, bms: BmsData) -> None:
    if len(data) < 6 or data[0] != 0xdd:
        return
    n = data[3] // 2
    bms.cell_voltages = [
        struct.unpack_from(">H", data, 4 + i * 2)[0] / 1000 for i in range(n)
    ]


def read_bms_serial(s: pyserial.Serial) -> Optional[BmsData]:
    import time
    try:
        s.reset_input_buffer()
        s.write(CMD_BASIC)
        time.sleep(0.1)
        bms = parse_basic(bytes(s.read(64)))
        if bms is None:
            return None
        s.write(CMD_CELLS)
        time.sleep(0.1)
        parse_cells(bytes(s.read(64)), bms)
        return bms
    except (pyserial.SerialException, OSError) as e:
        log.warning("Serial error: %s", e)
        return None


async def read_bms(mac: str) -> Optional[BmsData]:
    buf = bytearray()

    def on_notify(_, data):
        buf.extend(data)

    try:
        async with BleakClient(mac, timeout=CONNECT_TIMEOUT) as client:
            await client.start_notify(NOTIFY_UUID, on_notify)

            buf.clear()
            await client.write_gatt_char(WRITE_UUID, CMD_BASIC, response=False)
            await asyncio.sleep(0.5)
            bms = parse_basic(bytes(buf))
            if bms is None:
                return None

            buf.clear()
            await client.write_gatt_char(WRITE_UUID, CMD_CELLS, response=False)
            await asyncio.sleep(0.5)
            parse_cells(bytes(buf), bms)

            await client.stop_notify(NOTIFY_UUID)
            return bms
    except (BleakError, TimeoutError, OSError) as e:
        log.warning("BLE error for %s: %s", mac, e)
        return None


def publish_discovery(mqttc: mqtt.Client, device: dict, bms: BmsData) -> None:
    """Publish HA MQTT discovery config for all sensors of one BMS."""
    dev_id = device["name"]
    dev_meta = {
        "identifiers": [f"jbd_{dev_id}"],
        "name": device["label"],
        "manufacturer": "JBD",
        "model": "BMS",
    }
    state_topic = f"caravan/bms/{dev_id}/state"

    # (sid, name, unit, dev_class, state_class, template, precision)
    sensors = [
        ("voltage",      "Voltage",           "V",   "voltage",      "measurement", "{{ value_json.voltage }}",      2),
        ("current",      "Current",           "A",   "current",      "measurement", "{{ value_json.current }}",      2),
        ("soc",          "State of Charge",   "%",   "battery",      "measurement", "{{ value_json.soc }}",          0),
        ("remaining_ah", "Remaining Capacity","Ah",  None,           "measurement", "{{ value_json.remaining_ah }}", 2),
        ("nominal_ah",   "Nominal Capacity",  "Ah",  None,           "measurement", "{{ value_json.nominal_ah }}",   2),
    ]
    for i in range(len(bms.temps)):
        sensors.append((
            f"temp_{i+1}", f"Temperature {i+1}", "°C", "temperature", "measurement",
            f"{{{{ value_json.temp_{i+1} }}}}", 1,
        ))
    for i in range(len(bms.cell_voltages)):
        sensors.append((
            f"cell_{i+1}_voltage", f"Cell {i+1} Voltage", "V", "voltage", "measurement",
            f"{{{{ value_json.cell_{i+1}_voltage }}}}", 3,
        ))
    sensors.append((
        "last_updated", "Last Updated", None, "timestamp", None,
        "{{ value_json.last_updated }}", None,
    ))

    for sid, name, unit, dev_class, state_class, tmpl, precision in sensors:
        cfg = {
            "name": f"{device['label']} {name}",
            "state_topic": state_topic,
            "value_template": tmpl,
            "unique_id": f"jbd_{dev_id}_{sid}",
            "device": dev_meta,
        }
        if unit is not None:
            cfg["unit_of_measurement"] = unit
        if state_class is not None:
            cfg["state_class"] = state_class
        if precision is not None:
            cfg["suggested_display_precision"] = precision
        if dev_class:
            cfg["device_class"] = dev_class
        mqttc.publish(
            f"homeassistant/sensor/jbd_{dev_id}_{sid}/config",
            json.dumps(cfg),
            retain=True,
        )


def publish_state(mqttc: mqtt.Client, device: dict, bms: BmsData) -> None:
    payload = {
        "voltage":      round(bms.voltage, 2),
        "current":      round(bms.current, 2),
        "soc":          bms.soc,
        "remaining_ah": round(bms.remaining_ah, 2),
        "nominal_ah":   round(bms.nominal_ah, 2),
    }
    for i, t in enumerate(bms.temps):
        payload[f"temp_{i+1}"] = round(t, 1)
    for i, v in enumerate(bms.cell_voltages):
        payload[f"cell_{i+1}_voltage"] = round(v, 3)
    payload["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    mqttc.publish(f"caravan/bms/{device['name']}/state", json.dumps(payload), retain=True)
    log.info("%s: %sV  %sA  %s%%  %sAh", device["label"],
             payload["voltage"], payload["current"], payload["soc"], payload["remaining_ah"])


async def poll_serial(mqttc: mqtt.Client, device: dict, discovery_done: set) -> None:
    dev_id = device["name"]
    s = pyserial.Serial(device["serial_port"], baudrate=9600, timeout=1)
    log.info("Opened serial %s for %s", device["serial_port"], device["label"])
    try:
        while True:
            bms = await asyncio.get_event_loop().run_in_executor(None, read_bms_serial, s)
            if bms is None:
                log.warning("No data from %s", device["label"])
            else:
                if dev_id not in discovery_done:
                    publish_discovery(mqttc, device, bms)
                    discovery_done.add(dev_id)
                publish_state(mqttc, device, bms)
            await asyncio.sleep(POLL_INTERVAL_SERIAL)
    finally:
        s.close()


async def poll_ble(mqttc: mqtt.Client, device: dict, discovery_done: set) -> None:
    dev_id = device["name"]
    while True:
        log.info("Polling %s", device["label"])
        bms = await read_bms(device["mac"])
        if bms is None:
            log.warning("No data from %s", device["label"])
        else:
            if dev_id not in discovery_done:
                publish_discovery(mqttc, device, bms)
                discovery_done.add(dev_id)
            publish_state(mqttc, device, bms)
        await asyncio.sleep(POLL_INTERVAL_BLE)


async def main() -> None:
    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.connect(MQTT_HOST, MQTT_PORT)
    mqttc.loop_start()

    discovery_done: set = set()
    tasks = []
    for dev in DEVICES:
        if "serial_port" in dev:
            tasks.append(asyncio.create_task(poll_serial(mqttc, dev, discovery_done)))
        else:
            tasks.append(asyncio.create_task(poll_ble(mqttc, dev, discovery_done)))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
