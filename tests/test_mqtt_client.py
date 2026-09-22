"""Concurrent MQTT registration without connecting to a broker."""

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.config.settings import MQTTConfig
from src.services.mqtt.mqtt_client import MQTTClient


class MQTTClientConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.transport = Mock()
        config = MQTTConfig(
            enabled=True,
            host="broker.example.test",
            port=1883,
            username=None,
            password=None,
            client_id="edge-test",
            keepalive=60,
            heartbeat_interval_sec=30,
            topic_prefix="grn",
            qos=1,
            retain_presence=True,
            use_tls=False,
            agent_version="test",
        )
        with patch.object(MQTTClient, "_build_client", return_value=self.transport):
            self.client = MQTTClient(config)

    def test_subscribe_during_reconnect_preserves_ack_routes_and_callbacks(self):
        self.client.subscribe("config/desired", Mock())
        self.client.subscribe("config/request", Mock())
        listener = Mock()
        self.client.add_on_connect_listener(listener)
        restoring = threading.Event()
        resume = threading.Event()
        failures = []

        def subscribe(topic, **_kwargs):
            if topic == "config/desired":
                restoring.set()
                if not resume.wait(3):
                    raise TimeoutError("registration did not complete")

        self.transport.subscribe.side_effect = subscribe

        def connect():
            try:
                self.client._on_connect(self.transport, None, None, 0)
            except Exception as error:
                failures.append(error)

        worker = threading.Thread(target=connect, daemon=True)
        worker.start()
        try:
            self.assertTrue(restoring.wait(3))
            ack_handler = Mock()
            self.assertTrue(self.client.subscribe("capture/events/ack", ack_handler))
            self.assertTrue(self.client.subscribe("state/ack", ack_handler))
        finally:
            resume.set()
            worker.join(3)
        self.assertFalse(worker.is_alive(), "reconnect must not deadlock")
        self.assertEqual(failures, [])
        listener.assert_called_once_with()
        self.assertTrue(self.client.is_connected)
        for topic in ("config/desired", "config/request", "capture/events/ack", "state/ack"):
            self.transport.subscribe.assert_any_call(topic, qos=1)
        self.client._on_message(None, None, SimpleNamespace(topic="state/ack", payload=b"ack"))
        self.assertEqual(
            self.client._handler_queue.get_nowait(), (ack_handler, "state/ack", b"ack")
        )

    def test_connect_listener_can_register_handlers_and_next_cycle_listener(self):
        late_listener = Mock()
        calls = []

        def listener():
            calls.append("connect")
            self.client.subscribe("state/ack", Mock())
            if len(calls) == 1:
                self.client.add_on_connect_listener(late_listener)

        self.client.add_on_connect_listener(listener)
        self.client._on_connect(self.transport, None, None, 0)
        late_listener.assert_not_called()
        self.client._on_connect(self.transport, None, None, 0)
        late_listener.assert_called_once_with()

    def test_disconnect_listener_registration_takes_effect_next_cycle(self):
        late_listener = Mock()
        calls = []

        def listener(reason):
            calls.append(reason)
            self.client.subscribe("config/request", Mock())
            if len(calls) == 1:
                self.client.add_on_disconnect_listener(late_listener)

        self.client.add_on_disconnect_listener(listener)
        self.client._on_disconnect(self.transport, None, 1)
        late_listener.assert_not_called()
        self.client._on_disconnect(self.transport, None, 0)
        late_listener.assert_called_once_with("clean_disconnect")


if __name__ == "__main__":
    unittest.main()
