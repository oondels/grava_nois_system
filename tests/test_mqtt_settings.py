from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from src.config.settings import load_mqtt_config


class MQTTSettingsTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
