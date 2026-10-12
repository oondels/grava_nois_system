import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from src.services.config_restart import ConfigRestart
from src.services.docker_action_request import DockerActionRequestService
from src.security.durable_state import private_json


class ConfigRestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.actions = DockerActionRequestService(enabled=True, request_path=self.base / 'request.json', pull_token='', restart_token='')
        self.coordinator = ConfigRestart(self.base / 'intent.json', self.actions)
        now = datetime.now(timezone.utc)
        self.payload = dict(config_version=2, desired_hash='hash', correlation_id='correlation', issued_at=now.isoformat(), expires_at=(now+timedelta(seconds=120)).isoformat())

    def test_duplicates_reuse_admission_even_after_service_restart(self):
        first = self.coordinator.request(self.payload, {})
        second = ConfigRestart(self.coordinator.path, self.actions).request(self.payload, {})
        self.assertEqual(first['restart']['request_id'], second['restart']['request_id'])
        self.assertEqual(len(list((self.actions.action_root / 'requests').glob('*.json'))), 1)

    def test_interrupted_preparation_resumes_same_uuid(self):
        with patch.object(self.actions, 'submit_action', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                self.coordinator.request(self.payload, {})
        saved = self.coordinator.load()
        result = self.coordinator.reconcile(saved)
        self.assertEqual(result['restart']['request_id'], saved['restart']['request_id'])
        self.assertEqual(result['restart']['status'], 'queued')

    def test_missing_result_is_unknown_and_never_replayed(self):
        first = self.coordinator.request(self.payload, {})
        (self.actions.action_root / 'requests' / (first['restart']['request_id'] + '.json')).unlink()
        later = dict(self.payload, issued_at=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat())
        result = self.coordinator.request(later, {})
        self.assertEqual(result['restart']['status'], 'unknown')
        self.assertEqual(len(list((self.actions.action_root / 'requests').glob('*.json'))), 0)

    def test_explicit_new_envelope_retries_only_known_failure(self):
        first = self.coordinator.request(self.payload, {})
        key = first['restart']['request_id']
        (self.actions.action_root / 'requests' / (key + '.json')).unlink()
        private_json(self.actions.action_root / 'results' / (key + '.json'), dict(request_id=key, action='restart_container', status='error', error_code='command_exit_1', stage='docker_recreate'))
        self.assertEqual(self.coordinator.request(self.payload, {})['restart']['request_id'], key)
        later = dict(self.payload, issued_at=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat())
        self.assertNotEqual(self.coordinator.request(later, {})['restart']['request_id'], key)

    def test_confirmed_application_releases_obsolete_intent(self):
        first = self.coordinator.request(self.payload, {})
        (self.actions.action_root / 'requests' / (first['restart']['request_id'] + '.json')).unlink()
        self.coordinator.confirm_applied(2, 'hash')
        later = dict(self.payload, config_version=3, desired_hash='new-hash')
        second = self.coordinator.request(later, {})
        self.assertNotEqual(first['restart']['request_id'], second['restart']['request_id'])
        self.assertEqual(second['restart']['status'], 'queued')
