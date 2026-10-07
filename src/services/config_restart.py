"""Durable restart admission for authenticated remote configuration envelopes."""
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
from uuid import uuid4

from src.security.durable_state import private_json


class ConfigRestart:
    def __init__(self, path: Path, actions):
        self.path, self.actions = path, actions

    def load(self):
        return json.loads(self.path.read_text()) if self.path.exists() else None

    def confirm_applied(self, version, config_hash):
        record = self.load()
        if record and record['binding'][:2] == [version, config_hash]:
            record['restart'].update(status='succeeded', stage='applied', error_code='')
            private_json(self.path, record)

    def reconcile(self, record):
        metadata = record['restart']
        name = metadata['request_id'] + '.json'
        root = self.actions.action_root
        if metadata.get('stage') == 'applied':
            return record
        result_path = root / 'results' / name
        if not result_path.exists():
            result_path = root.parent / 'device-operations' / 'host-results' / name
        if result_path.exists():
            result = json.loads(result_path.read_text())
            if result.get('request_id') == metadata['request_id'] and result.get('action') == 'restart_container':
                metadata.update(status={'ok': 'succeeded', 'error': 'failed'}.get(result.get('status'), 'unknown'),
                                error_code=result.get('error_code') or '', stage=result.get('stage') or '')
        elif (root / 'processing' / name).exists():
            metadata['status'] = 'running'
        elif (root / 'requests' / name).exists():
            metadata['status'] = 'queued'
        elif metadata['status'] == 'prepared':
            submission = self.actions.submit_action('restart_container', source='remote_config',
                request_id=metadata['request_id'], expires_at=record['expires_at'])
            metadata.update(status='queued' if submission.accepted else ('unknown' if submission.code == 'uncertain' else 'failed'),
                            error_code='' if submission.accepted else submission.code)
        elif metadata['status'] in {'queued', 'running'}:
            # A receipt without the result cannot prove execution. Never replay an effect.
            metadata.update(status='unknown', error_code='restart_result_unavailable')
        private_json(self.path, record)
        return record

    def request(self, payload, report):
        record = self.load()
        binding = [payload['config_version'], payload['desired_hash'], payload['correlation_id']]
        if record:
            record = self.reconcile(record)
            same = record['binding'] == binding
            failed = record['restart']['status'] == 'failed'
            newer = datetime.fromisoformat(payload['issued_at'].replace('Z', '+00:00')) > datetime.fromisoformat(record['issued_at'].replace('Z', '+00:00'))
            if same and not (failed and newer):
                return record
            if record['restart']['status'] in {'prepared', 'queued', 'running', 'unknown'}:
                # An unresolved older effect also prevents a concurrent new restart.
                return record
        expires = min(datetime.fromisoformat(payload['expires_at'].replace('Z', '+00:00')),
                      datetime.now(timezone.utc) + timedelta(seconds=120))
        record = {'binding': binding, 'issued_at': payload['issued_at'], 'expires_at': expires.isoformat(),
                  'report': report, 'restart': {'request_id': str(uuid4()), 'status': 'prepared', 'stage': 'admission', 'error_code': ''}}
        private_json(self.path, record)  # Commit intent before admission; recovery reuses the UUID.
        return self.reconcile(record)
