"""Docker liveness probe; --ready also requires configured camera buffers."""

import argparse

from src.services.runtime_health import healthy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready", action="store_true")
    args = parser.parse_args()
    return 0 if healthy(require_cameras=args.ready) else 1


if __name__ == "__main__":
    raise SystemExit(main())
