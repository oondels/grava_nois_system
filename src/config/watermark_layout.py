"""Versioned branding contract shared with the administrative editor."""
import math
import re
from uuid import UUID
SLOTS = ('institutional', 'clientBottom', 'clientTop')


def validate_layout(layout):
    def require(condition, code):
        if not condition:
            raise ValueError(code)
    require(isinstance(layout, dict) and set(layout) == {'version', 'clientEnabled', 'assets', 'default', 'cameras'}, 'invalid_watermark_layout')
    require(layout['version'] == 2 and isinstance(layout['clientEnabled'], bool), 'invalid_watermark_version')
    assets = layout['assets']
    require(isinstance(assets, dict) and set(assets) == set(SLOTS), 'invalid_watermark_assets')
    for asset in assets.values():
        if asset is None:
            continue
        require(isinstance(asset, dict) and set(asset) == {'id', 'sha256', 'width', 'height'}, 'invalid_watermark_asset')
        require(str(UUID(asset['id'])) == asset['id'], 'invalid_asset_id')
        require(isinstance(asset['sha256'], str) and re.fullmatch('[a-f0-9]{64}', asset['sha256']), 'invalid_asset_hash')
        require(all(type(asset[k]) is int and 1 <= asset[k] <= 2048 for k in ('width', 'height')), 'invalid_asset_size')
    require(isinstance(layout['cameras'], dict) and all(isinstance(k, str) and 1 <= len(k) <= 255 for k in layout['cameras']), 'invalid_watermark_cameras')
    for items in [layout['default'], *layout['cameras'].values()]:
        require(isinstance(items, dict) and set(items) == set(SLOTS), 'invalid_watermark_placements')
        for slot, p in items.items():
            require(isinstance(p, dict) and set(p) == {'enabled', 'x', 'y', 'width', 'height', 'opacity'}, 'invalid_watermark_placement')
            require(isinstance(p['enabled'], bool), 'invalid_watermark_enabled')
            require(all(type(p[k]) in (int, float) and math.isfinite(p[k]) and 0 <= p[k] <= 1 for k in ('x', 'y', 'width', 'height', 'opacity')), 'invalid_watermark_number')
            require(p['width'] > 0 and p['height'] > 0 and p['x'] + p['width'] <= 1 + 1e-9 and p['y'] + p['height'] <= 1 + 1e-9, 'watermark_outside_frame')
            require(not p['enabled'] or assets[slot] is not None, 'watermark_asset_required')
        require(items['institutional']['enabled'] and items['institutional']['opacity'] > 0, 'institutional_watermark_required')
        for i, slot in enumerate(SLOTS):
            a = items[slot]
            for other in SLOTS[i+1:]:
                b = items[other]
                require(not (a['enabled'] and b['enabled'] and min(a['x']+a['width'], b['x']+b['width'])-max(a['x'], b['x']) > 1e-9 and min(a['y']+a['height'], b['y']+b['height'])-max(a['y'], b['y']) > 1e-9), 'watermark_overlap')
    return layout


def image_rect(placement, image_width, image_height, width, height):
    """Round inward to prevent adjacent boxes overlapping after pixel conversion."""
    x0, y0 = math.ceil(placement['x'] * width - 1e-9), math.ceil(placement['y'] * height - 1e-9)
    x1 = math.floor((placement['x'] + placement['width']) * width + 1e-9)
    y1 = math.floor((placement['y'] + placement['height']) * height + 1e-9)
    scale = min((x1-x0)/image_width, (y1-y0)/image_height)
    w, h = math.floor(image_width*scale + 1e-9), math.floor(image_height*scale + 1e-9)
    if min(w, h) < 1:
        raise ValueError('watermark_too_small_for_output')
    return x0+(x1-x0-w)//2, y0+(y1-y0-h)//2, w, h
