#!/usr/bin/env python3
"""Starlink dish stats MQTT collector with Home Assistant auto-discovery."""

import json
import logging
import time
from datetime import datetime, timezone

import grpc
import paho.mqtt.client as mqtt
from yagrc import reflector as yagrc_reflector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("starlink")

MQTT_HOST     = "localhost"
MQTT_PORT     = 1883
POLL_INTERVAL = 5
STARLINK_HOST = "192.168.100.1:9200"
DEVICE_NAME   = "starlink"
DEVICE_LABEL  = "Starlink"

STATE_TOPIC = f"caravan/starlink/{DEVICE_NAME}/state"
DEV_META = {
    "identifiers": ["starlink_dish"],
    "name": DEVICE_LABEL,
    "manufacturer": "SpaceX",
    "model": "Starlink Dish",
}

SENSORS = [
    # (sid, name, unit, device_class, state_class, template, precision)
    ("downlink_mbps",      "Download",             "Mbit/s", "data_rate",  "measurement",     "{{ value_json.downlink_mbps }}",      1),
    ("uplink_mbps",        "Upload",               "Mbit/s", "data_rate",  "measurement",     "{{ value_json.uplink_mbps }}",        1),
    ("ping_latency_ms",    "Ping Latency",         "ms",     None,         "measurement",     "{{ value_json.ping_latency_ms }}",    1),
    ("ping_drop_rate",     "Ping Drop Rate",       "%",      None,         "measurement",     "{{ value_json.ping_drop_rate }}",     1),
    ("obstruction",        "Obstruction",          "%",      None,         "measurement",     "{{ value_json.obstruction }}",        2),
    ("power_w",            "Power",                "W",      "power",      "measurement",     "{{ value_json.power_w }}",            1),
    ("uptime_h",           "Uptime",               "h",      "duration",   "total_increasing","{{ value_json.uptime_h }}",           1),
    ("country_code",       "Country",              None,     None,         None,              "{{ value_json.country_code }}",       None),
    ("dl_restriction",     "DL Restriction",       None,     None,         None,              "{{ value_json.dl_restriction }}",     None),
    ("ul_restriction",     "UL Restriction",       None,     None,         None,              "{{ value_json.ul_restriction }}",     None),
    ("snr_above_noise",    "SNR OK",               None,     None,         None,              "{{ value_json.snr_above_noise }}",    None),
    ("alerts",             "Alerts",               None,     None,         None,              "{{ value_json.alerts }}",             None),
    ("last_updated",       "Last Updated",         None,     "timestamp",  None,              "{{ value_json.last_updated }}",       None),
]

ALERT_FIELDS = [
    "motors_stuck", "thermal_throttle", "thermal_shutdown",
    "mast_not_near_vertical", "unexpected_location", "slow_ethernet_speeds",
    "roaming", "install_pending", "is_heating", "power_supply_thermal_throttle",
    "is_power_save_idle", "dish_water_detected", "no_ethernet_link",
    "lower_signal_than_predicted", "obstruction_map_reset",
]


def publish_discovery(mqttc: mqtt.Client) -> None:
    for sid, name, unit, dev_class, state_class, tmpl, precision in SENSORS:
        cfg = {
            "name": f"{DEVICE_LABEL} {name}",
            "state_topic": STATE_TOPIC,
            "value_template": tmpl,
            "unique_id": f"starlink_{DEVICE_NAME}_{sid}",
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
            f"homeassistant/sensor/starlink_{DEVICE_NAME}_{sid}/config",
            json.dumps(cfg),
            retain=True,
        )


def make_stub():
    channel = grpc.insecure_channel(STARLINK_HOST)
    ref = yagrc_reflector.GrpcReflectionClient()
    ref.load_protocols(channel, symbols=["SpaceX.API.Device.Device"])
    stub = ref.service_stub_class("SpaceX.API.Device.Device")(channel)
    Request = ref.message_class("SpaceX.API.Device.Request")
    GetStatusRequest = ref.message_class("SpaceX.API.Device.GetStatusRequest")
    GetHistoryRequest = ref.message_class("SpaceX.API.Device.GetHistoryRequest")
    return stub, Request, GetStatusRequest, GetHistoryRequest


def read_starlink(stub, Request, GetStatusRequest, GetHistoryRequest) -> dict | None:
    try:
        resp = stub.Handle(Request(get_status=GetStatusRequest()), timeout=5)
        s = resp.dish_get_status

        active_alerts = [f for f in ALERT_FIELDS if getattr(s.alerts, f, False)]

        data = {
            "downlink_mbps":   round(s.downlink_throughput_bps / 1e6, 1),
            "uplink_mbps":     round(s.uplink_throughput_bps / 1e6, 1),
            "ping_latency_ms": round(s.pop_ping_latency_ms, 1),
            "ping_drop_rate":  round(s.pop_ping_drop_rate * 100, 1),
            "obstruction":     round(s.obstruction_stats.fraction_obstructed * 100, 2),
            "uptime_h":        round(s.device_state.uptime_s / 3600, 1),
            "country_code":    s.device_info.country_code,
            "dl_restriction":  str(s.dl_bandwidth_restricted_reason).replace("BANDWIDTH_RESTRICTION_REASON_", ""),
            "ul_restriction":  str(s.ul_bandwidth_restricted_reason).replace("BANDWIDTH_RESTRICTION_REASON_", ""),
            "snr_above_noise": s.is_snr_above_noise_floor,
            "alerts":          ", ".join(active_alerts) if active_alerts else "none",
        }

        # Power from history (most recent sample)
        try:
            hist = stub.Handle(Request(get_history=GetHistoryRequest()), timeout=5)
            power_samples = list(hist.dish_get_history.power_in)
            if power_samples:
                data["power_w"] = round(power_samples[-1], 1)
        except grpc.RpcError:
            data["power_w"] = None

        return data
    except grpc.RpcError as e:
        log.warning("gRPC error: %s", e)
        return None


def main() -> None:
    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.connect(MQTT_HOST, MQTT_PORT)
    mqttc.loop_start()

    stub, Request, GetStatusRequest, GetHistoryRequest = make_stub()
    discovery_done = False

    while True:
        data = read_starlink(stub, Request, GetStatusRequest, GetHistoryRequest)
        if data:
            if not discovery_done:
                publish_discovery(mqttc)
                discovery_done = True
            data["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
            mqttc.publish(STATE_TOPIC, json.dumps(data), retain=True)
            log.info("↓%s Mbit/s  ↑%s Mbit/s  ping=%sms  drop=%s%%  %sW  alerts=%s",
                     data["downlink_mbps"], data["uplink_mbps"],
                     data["ping_latency_ms"], data["ping_drop_rate"],
                     data["power_w"], data["alerts"])
        else:
            log.warning("No data from Starlink")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
