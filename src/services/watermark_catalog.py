"""Signed, bounded image catalog transport; never accepts remote filesystem paths."""
import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import urllib.request
from urllib.parse import urlparse
from src.security.request_signer import sign_request
from src.config.watermark_layout import SLOTS, validate_layout


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('watermark_redirect_rejected')


def post(action, payload):
    base = (os.getenv('GN_API_BASE') or os.getenv('API_BASE_URL') or '').rstrip('/')
    url = urlparse(base)
    local = os.getenv('GN_MAINTENANCE_ALLOW_INSECURE_HTTP', '0').lower() in {'1', 'true', 'yes', 'on'}
    if not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('watermark_invalid_api_base')
    if url.scheme != 'https' and not (url.scheme == 'http' and local and os.getenv('NODE_ENV') != 'production'):
        raise ValueError('watermark_requires_https')
    path = '/api/maintenance/watermarks/' + action
    body = json.dumps(payload, separators=(',', ':'))
    signed = sign_request(method='POST', path=url.path + path, body_string=body,
        device_id=os.getenv('DEVICE_ID') or os.getenv('GN_DEVICE_ID'),
        device_secret=os.getenv('DEVICE_SECRET') or os.getenv('GN_DEVICE_SECRET'),
        client_id=os.getenv('GN_CLIENT_ID') or os.getenv('CLIENT_ID') or None, content_type='application/json')
    try:
        req = urllib.request.Request(base + path, data=body.encode(), headers=signed.headers, method='POST')
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
            data = json.loads(response.read(3 * 1024 * 1024))
        if not data.get('success') or not isinstance(data.get('data'), dict):
            raise ValueError()
        return data['data']
    except Exception:
        raise ValueError('watermark_catalog_transfer_failed') from None


def validate_bytes(content, asset):
    if len(content) > 2097152 or len(content) < 24 or content[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('watermark_asset_invalid')
    if hashlib.sha256(content).hexdigest() != asset['sha256'] or struct.unpack('>II', content[16:24]) != (asset['width'], asset['height']):
        raise ValueError('watermark_asset_integrity')


def prepare_assets(layout, directory):
    validate_layout(layout)
    versions = Path(directory) / 'client-versions'
    if Path(directory).is_symlink() or versions.is_symlink():
        raise ValueError('watermark_symlink_rejected')
    versions.mkdir(parents=True, exist_ok=True)
    for asset in layout['assets'].values():
        if asset is None:
            continue
        target = versions / (asset['sha256'] + '.png')
        if target.is_symlink():
            raise ValueError('watermark_symlink_rejected')
        if target.exists():
            validate_bytes(target.read_bytes(), asset)
            continue
        reply = post('read', {'id': asset['id']})
        content = base64.b64decode(reply['content'], validate=True)
        validate_bytes(content, asset)
        fd, name = tempfile.mkstemp(dir=versions, prefix='.asset-')
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
            os.chmod(name, 0o644)
            os.replace(name, target)
            fd = os.open(versions, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
        finally:
            Path(name).unlink(missing_ok=True)


def verify_local_assets(layout, directory):
    """Verify every staged asset before restoring a pending configuration; never download at boot."""
    validate_layout(layout)
    directory = Path(directory)
    versions = directory / 'client-versions'
    if directory.is_symlink() or versions.is_symlink():
        raise ValueError('watermark_symlink_rejected')
    for asset in layout['assets'].values():
        if asset is None:
            continue
        target = versions / (asset['sha256'] + '.png')
        if target.is_symlink():
            raise ValueError('watermark_symlink_rejected')
        validate_bytes(target.read_bytes(), asset)


def snapshot_layout(layout, camera_id, directory):
    """No network in processing. Config activation already staged verified immutable bytes."""
    import copy
    validate_layout(layout)
    selected = copy.deepcopy(layout['cameras'].get(camera_id, layout['default']))
    paths = {}
    for slot in SLOTS:
        if slot != 'institutional' and not layout['clientEnabled']:
            selected[slot]['enabled'] = False
        asset = layout['assets'][slot]
        if not selected[slot]['enabled']:
            paths[slot] = None
            continue
        target = Path(directory) / 'client-versions' / (asset['sha256'] + '.png')
        if target.is_symlink() or target.parent.is_symlink():
            raise ValueError('watermark_symlink_rejected')
        validate_bytes(target.read_bytes(), asset)
        selected[slot]['imageWidth'], selected[slot]['imageHeight'] = asset['width'], asset['height']
        paths[slot] = str(target)
    return {'version': 2, 'placements': selected, 'assets': copy.deepcopy(layout['assets'])}, paths


def sync_inventory(config, directory, camera_directories):
    from src.video.watermark_assets import WatermarkAssets
    from src.video.processor import ffprobe_metadata
    directory = Path(directory)
    def first(*names):
        return next((directory / name for name in names if (directory / name).is_file()), None)
    wm = config.get('processing', {}).get('watermark', {}).get('layout')
    if wm:
        assets = wm['assets']
    else:
        bottom, top = WatermarkAssets(directory, first('client_logo_wm.png', 'client_logo.png'), first('client_logo_top_wm.png', 'client_logo_top.png')).resolve(respect_enabled=False)
        # Inventory includes disabled client images so the editor can enable them.
        if bottom is None: bottom = first('client_logo_wm.png', 'client_logo.png')
        if top is None: top = first('client_logo_top_wm.png', 'client_logo_top.png')
        paths = [first('replay_grava_nois_wm.png', 'replay_grava_nois.png'), bottom, top]
        assets = {}
        for slot, path in zip(SLOTS, paths):
            if path and not path.is_symlink() and path.stat().st_size <= 2097152:
                assets[slot] = post('import', {'slot': slot, 'content': base64.b64encode(path.read_bytes()).decode()})
            else: assets[slot] = None
    frames = {}
    for camera_id, folder in camera_directories.items():
        candidates = sorted(Path(folder).glob('*.ts'), key=lambda p: p.stat().st_mtime, reverse=True)
        # Skip newest segment (may still be written).
        for candidate in candidates[1:4]:
            try:
                meta = ffprobe_metadata(candidate)
                w, h = int(meta['width']), int(meta['height'])
                if w and h:
                    frames[camera_id] = {'width': w, 'height': h}; break
            except Exception:
                continue
    post('runtime', {'layoutVersion': 2, 'clientEnabled': wm['clientEnabled'] if wm else os.getenv('GN_CLIENT_WATERMARK_ENABLED', '1').lower() in {'1', 'true', 'yes', 'on'}, 'assets': assets, 'frames': frames})
