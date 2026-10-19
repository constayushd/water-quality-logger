#!/usr/bin/env python3
"""
wq_mqtt.py -- the bit of MQTT both ends need. Not run directly.

Imported by pi_uplink.py (on the Pi) and pc_dashboard.py (on the PC). Keeps the
broker settings, the paho version differences and the topic names in one place.

Configuration comes from, in order of precedence:

    1. command-line flags
    2. environment variables   WQ_MQTT_HOST, WQ_MQTT_PORT, WQ_MQTT_USER,
                               WQ_MQTT_PASSWORD, WQ_MQTT_TOPIC, WQ_DEVICE_ID
    3. an ini file             wq_mqtt.ini  (chmod 600 it -- it holds a password)

    [mqtt]
    host = abc123....s1.eu.hivemq.cloud
    port = 8883
    username = wq_pi
    password = ...
    tls = true
    websockets = false
    base_topic = wq
    device_id = pi-01
"""

from __future__ import annotations

import configparser
import os
import ssl
import sys
from dataclasses import dataclass
from typing import Any, Optional, Tuple

SCHEMA = 1
DEFAULT_CONFIG = "wq_mqtt.ini"


# --------------------------------------------------------------------------
@dataclass
class MqttConfig:
    host: str = ""
    port: int = 8883
    username: str = ""
    password: str = ""
    tls: bool = True
    websockets: bool = False
    base_topic: str = "wq"
    device_id: str = "pi-01"
    ca_certs: Optional[str] = None
    insecure_tls: bool = False

    # ---- topics ----
    def data_topic(self, device: Optional[str] = None) -> str:
        return f"{self.base_topic}/{device or self.device_id}/data"

    def status_topic(self, device: Optional[str] = None) -> str:
        return f"{self.base_topic}/{device or self.device_id}/status"

    def subscribe_data(self) -> str:
        return f"{self.base_topic}/+/data"

    def subscribe_status(self) -> str:
        return f"{self.base_topic}/+/status"

    # ---- loading ----
    @classmethod
    def load(cls, path: Optional[str] = None, **overrides: Any) -> "MqttConfig":
        cfg = cls()
        path = path or os.environ.get("WQ_MQTT_CONFIG") or DEFAULT_CONFIG
        if os.path.exists(path):
            parser = configparser.ConfigParser()
            parser.read(path)
            if parser.has_section("mqtt"):
                sec = parser["mqtt"]
                cfg.host = sec.get("host", cfg.host)
                cfg.port = sec.getint("port", cfg.port)
                cfg.username = sec.get("username", cfg.username)
                cfg.password = sec.get("password", cfg.password)
                cfg.tls = sec.getboolean("tls", cfg.tls)
                cfg.websockets = sec.getboolean("websockets", cfg.websockets)
                cfg.base_topic = sec.get("base_topic", cfg.base_topic)
                cfg.device_id = sec.get("device_id", cfg.device_id)
                cfg.ca_certs = sec.get("ca_certs", cfg.ca_certs) or None
                cfg.insecure_tls = sec.getboolean("insecure_tls", cfg.insecure_tls)

        env = os.environ.get
        cfg.host = env("WQ_MQTT_HOST", cfg.host)
        cfg.port = int(env("WQ_MQTT_PORT", cfg.port))
        cfg.username = env("WQ_MQTT_USER", cfg.username)
        cfg.password = env("WQ_MQTT_PASSWORD", cfg.password)
        cfg.base_topic = env("WQ_MQTT_TOPIC", cfg.base_topic)
        cfg.device_id = env("WQ_DEVICE_ID", cfg.device_id)

        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg

    def check(self) -> None:
        if not self.host:
            raise SystemExit(
                "No broker configured.\n"
                f"  Create {DEFAULT_CONFIG} (see the header of wq_mqtt.py),\n"
                "  or pass --broker HOST --mqtt-user USER --mqtt-pass SECRET,\n"
                "  or set WQ_MQTT_HOST / WQ_MQTT_USER / WQ_MQTT_PASSWORD."
            )

    def describe(self) -> str:
        scheme = "wss" if self.websockets else ("mqtts" if self.tls else "mqtt")
        return f"{scheme}://{self.host}:{self.port}/{self.base_topic}"


# --------------------------------------------------------------------------
def add_arguments(ap: Any, side: str) -> None:
    """Add the broker flags to an argparse parser. side is 'pi' or 'pc'."""
    g = ap.add_argument_group("broker")
    g.add_argument("--config", metavar="INI", help=f"ini file (default {DEFAULT_CONFIG})")
    g.add_argument("--broker", metavar="HOST", help="broker hostname")
    g.add_argument("--broker-port", type=int, metavar="N",
                   help="8883 for TLS, 8884 for TLS over websockets, 1883 plain")
    g.add_argument("--mqtt-user", metavar="USER")
    g.add_argument("--mqtt-pass", metavar="SECRET")
    g.add_argument("--topic", metavar="BASE", help="base topic (default wq)")
    g.add_argument("--no-tls", action="store_true", help="plain MQTT, testing only")
    g.add_argument("--websockets", action="store_true",
                   help="tunnel MQTT over websockets -- use when a network "
                        "blocks port 8883 but allows ordinary web traffic")
    if side == "pi":
        g.add_argument("--device-id", metavar="ID", help="this Pi's name (default pi-01)")


def config_from_args(args: Any) -> MqttConfig:
    overrides = {
        "host": getattr(args, "broker", None),
        "port": getattr(args, "broker_port", None),
        "username": getattr(args, "mqtt_user", None),
        "password": getattr(args, "mqtt_pass", None),
        "base_topic": getattr(args, "topic", None),
        "device_id": getattr(args, "device_id", None),
    }
    cfg = MqttConfig.load(getattr(args, "config", None), **overrides)
    if getattr(args, "no_tls", False):
        cfg.tls = False
    if getattr(args, "websockets", False):
        cfg.websockets = True
        if not getattr(args, "broker_port", None):
            cfg.port = 8884 if cfg.tls else 8083
    return cfg


# --------------------------------------------------------------------------
def make_client(cfg: MqttConfig, client_id: str, clean_session: bool = True) -> Tuple[Any, Any]:
    """Build a paho client that works on both paho-mqtt 1.x and 2.x.

    Returns (client, mqtt_module). Callbacks use the 1.x signatures throughout.
    """
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        raise SystemExit(
            "paho-mqtt is not installed.\n"
            "    pip install paho-mqtt            (or: sudo apt install python3-paho-mqtt)"
        )

    transport = "websockets" if cfg.websockets else "tcp"
    try:
        # paho 2.x insists on being told which callback API you want.
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION1,
            client_id=client_id,
            clean_session=clean_session,
            transport=transport,
        )
    except (AttributeError, TypeError):
        client = mqtt.Client(client_id=client_id, clean_session=clean_session,
                             transport=transport)

    if cfg.username:
        client.username_pw_set(cfg.username, cfg.password)
    if cfg.tls:
        client.tls_set(ca_certs=cfg.ca_certs, cert_reqs=ssl.CERT_REQUIRED,
                       tls_version=ssl.PROTOCOL_TLS_CLIENT)
        if cfg.insecure_tls:
            client.tls_insecure_set(True)
            print("! TLS certificate checking is OFF -- testing only", file=sys.stderr)
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    return client, mqtt


def explain_rc(rc: int) -> str:
    return {
        0: "connected",
        1: "refused: the broker does not speak this MQTT version",
        2: "refused: client id rejected",
        3: "refused: the broker is unavailable",
        4: "refused: bad username or password",
        5: "refused: not authorised -- check the credential's topic permissions",
    }.get(rc, f"refused: code {rc}")
