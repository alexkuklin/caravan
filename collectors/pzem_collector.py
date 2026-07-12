#!/usr/bin/env python3
"""PZEM-017 DC shunt MQTT collector with Home Assistant auto-discovery."""

import json
import logging
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("pzem")

MQTT_HOST     = "localhost"
MQTT_PORT     = 1883
POLL_INTERVAL = 1
SERIAL_PORT   = "/dev/ttyPZEM"
SLAVE_ADDR    = 1
DEVICE_NAME   = "shunt"
DEVICE_LABEL  = "DC Shunt"

STATE_TOPIC = f"caravan/pzem/{DEVICE_NAME}/state"
DEV_META = {
    "identifiers": [f"pzem_{DEVICE_NAME}"],
    "name": DEVICE_LABEL,
    "manufacturer": "PZEM",
    "model": "PZEM-017",
}

SENSORS = [
    # (sid, name, unit, device_class, state_class, template, precision)
    ("voltage", "Voltage", "V",   "voltage", "measurement", "{{ value_json.voltage }}", 2),
    ("current", "Current", "A",   "current", "measurement", "{{ value_json.current }}", 2),
    ("power",   "Power",   "W",   "power",   "measurement", "{{ value_json.power }}",   1),
    ("energy",  "Energy",  "Wh",  "energy",  "total_increasing", "{{ value_json.energy }}", 0),
    ("last_updated", "Last Updated", None, "timestamp", None, "{{ value_json.last_updated }}", None),
]


def publish_discovery(mqttc: mqtt.Client) -> None:
    for sid, name, unit, dev_class, state_class, tmpl, precision in SENSORS:
        cfg = {
            "name": f"{DEVICE_LABEL} {name}",
            "state_topic": STATE_TOPIC,
            "value_template": tmpl,
            "unique_id": f"pzem_{DEVICE_NAME}_{sid}",
            "device": DEV_META,
        }
        if unit:
            cfg["unit_of_measurement"] = unit
        if state_class:
            cfg["state_class"] = state_class
        if precision is not None:
            cfg["suggested_display_precision"] = precision
        if dev_class:
            cfg["device_class"] = dev_class
        mqttc.publish(
            f"homeassistant/sensor/pzem_{DEVICE_NAME}_{sid}/config",
            json.dumps(cfg),
            retain=True,
        )


def read_pzem(client: ModbusSerialClient) -> dict | None:
    try:
        r = client.read_input_registers(address=0x0000, count=8, slave=SLAVE_ADDR)
        regs = r.registers
        voltage = regs[0] * 0.01
        current = regs[1] * 0.01
        power   = ((regs[3] << 16) | regs[2]) * 0.1
        energy  = (regs[5] << 16) | regs[4]
        return {"voltage": round(voltage, 2), "current": round(current, 2),
                "power": round(power, 1), "energy": energy}
    except ModbusException as e:
        log.warning("Modbus error: %s", e)
        return None


def main() -> None:
    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.connect(MQTT_HOST, MQTT_PORT)
    mqttc.loop_start()

    client = ModbusSerialClient(
        port=SERIAL_PORT, baudrate=9600, bytesize=8,
        parity="N", stopbits=2, timeout=1,
    )
    client.connect()

    discovery_done = False
    while True:
        data = read_pzem(client)
        if data:
            if not discovery_done:
                publish_discovery(mqttc)
                discovery_done = True
            data["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
            mqttc.publish(STATE_TOPIC, json.dumps(data), retain=True)
            log.info("%s: %sV  %sA  %sW  %sWh",
                     DEVICE_LABEL, data["voltage"], data["current"],
                     data["power"], data["energy"])
        else:
            log.warning("No data from %s", DEVICE_LABEL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
