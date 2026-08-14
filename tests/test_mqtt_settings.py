from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import src.config.config_loader as config_loader
from src.config.config_loader import reset_config_cache
from src.config.config_schema import validate_config_dict
from src.config.settings import load_mqtt_config


class MQTTSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_config_dir = tempfile.TemporaryDirectory()
        self._default_config_path_patch = patch.object(
            config_loader,
            "_DEFAULT_CONFIG_PATH",
            Path(self._tmp_config_dir.name) / "config.json",
        )
        self._default_config_path_patch.start()
        reset_config_cache()

    def tearDown(self) -> None:
        reset_config_cache()
        self._default_config_path_patch.stop()
        self._tmp_config_dir.cleanup()

    def test_defaults_to_disabled_when_not_configured(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_mqtt_config()

        self.assertFalse(config.enabled)
        self.assertEqual(config.host, "")
        self.assertEqual(config.port, 1883)
        self.assertEqual(config.topic_prefix, "grn")

    def test_parses_broker_url_and_tls(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GN_MQTT_ENABLED": "1",
                "GN_MQTT_BROKER_URL": "mqtts://broker.example.com:8883",
                "DEVICE_ID": "edge-01",
                "GN_AGENT_VERSION": "1.4.0",
            },
            clear=True,
        ):
            config = load_mqtt_config()

        self.assertTrue(config.enabled)
        self.assertEqual(config.host, "broker.example.com")
        self.assertEqual(config.port, 8883)
        self.assertTrue(config.use_tls)
        self.assertEqual(config.client_id, "edge-01")
        self.assertEqual(config.agent_version, "1.4.0")

    def test_explicit_host_and_port_override_url_defaults(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GN_MQTT_ENABLED": "true",
                "GN_MQTT_HOST": "broker.internal",
                "GN_MQTT_PORT": "1884",
                "GN_MQTT_QOS": "2",
            },
            clear=True,
        ):
            config = load_mqtt_config()

        self.assertEqual(config.host, "broker.internal")
        self.assertEqual(config.port, 1884)
        self.assertEqual(config.qos, 2)

    def test_rejects_websocket_and_unsupported_broker_url_schemes(self) -> None:
        for scheme in ("ws", "wss", "http", "ssl", "tls"):
            with self.subTest(scheme=scheme):
                reset_config_cache()
                with (
                    patch.dict(
                        os.environ,
                        {
                            "GN_MQTT_ENABLED": "1",
                            "GN_MQTT_BROKER_URL": f"{scheme}://broker.example.com:8883",
                        },
                        clear=True,
                    ),
                    self.assertRaisesRegex(
                        ValueError,
                        "aceita somente mqtt:// ou mqtts://",
                    ),
                ):
                    load_mqtt_config()

    def test_managed_broker_host_rejects_url_instead_of_treating_it_as_tcp_host(self) -> None:
        errors = validate_config_dict(
            {
                "mqtt": {
                    "broker": {
                        "host": "wss://broker.example.com/mqtt",
                        "port": 8883,
                        "tls": True,
                    }
                }
            }
        )

        self.assertTrue(any("mqtt.broker.host deve conter somente hostname" in e for e in errors))

    def test_topic_for_rejects_device_id_with_topic_separators_or_wildcards(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GN_MQTT_ENABLED": "1",
                "GN_MQTT_HOST": "broker.internal",
                "DEVICE_ID": "edge-01",
            },
            clear=True,
        ):
            config = load_mqtt_config()

        self.assertEqual(
            config.topic_for("edge-01", "heartbeat"),
            "grn/devices/edge-01/heartbeat",
        )

        for invalid_device_id in ("edge/01", "edge+01", "edge#01"):
            with self.subTest(invalid_device_id=invalid_device_id):
                with self.assertRaises(ValueError):
                    config.topic_for(invalid_device_id, "heartbeat")


if __name__ == "__main__":
    unittest.main()
