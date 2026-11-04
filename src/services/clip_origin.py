"""Build ingest origin only from the durable capture snapshot."""


def clip_origin(metadata, default_venue, camera_id=None):
    origin = {"venue_id": metadata.get("capture_venue_id") or default_venue}
    if metadata.get("camera_metadata_enabled") or metadata.get("camera_assignment_enabled"):
        camera = camera_id if camera_id is not None else metadata.get("camera_id")
        if not isinstance(camera, str) or not camera.strip() or len(camera) > 120:
            raise ValueError("invalid persisted camera_id")
        origin["camera_id"] = camera
    return origin
