#!/usr/bin/env python3
"""Run code tests without local credentials, real cameras or outbound sockets.

Usage: .venv/bin/python scripts/test_isolated.py [--coverage] [pytest arguments]
Real-camera and service integration harnesses are separate explicit opt-ins.
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import socket
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", action="store_true")
    options, pytest_args = parser.parse_known_args()
    repo = Path(__file__).resolve().parents[1]
    os.chdir(repo)
    sys.path.insert(0, str(repo))
    test_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    config_repo = os.environ.get("GN_CONFIG_REPO")
    with tempfile.TemporaryDirectory(prefix="gn-isolated-") as temporary:
        os.environ.clear()
        os.environ.update(
            PATH=test_path,
            PYTHONDONTWRITEBYTECODE="1",
            GN_LOG_DIR=temporary,
            GN_CONFIG_PATH=str(Path(temporary) / "config.json"),
            GN_RUNTIME_CONFIG_DIR=str(Path(temporary) / "runtime"),
            GN_RUN_CAMERA_INTEGRATION="0",
            COVERAGE_FILE=str(Path(temporary) / ".coverage"),
        )
        if config_repo:
            os.environ["GN_CONFIG_REPO"] = config_repo
        import dotenv

        dotenv.load_dotenv = lambda *args, **kwargs: False
        logging.disable(logging.CRITICAL)
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex

        def connect(stream, address):
            if stream.family in (socket.AF_INET, socket.AF_INET6):
                raise OSError("network disabled in isolated tests")
            return original_connect(stream, address)

        def connect_ex(stream, address):
            if stream.family in (socket.AF_INET, socket.AF_INET6):
                raise OSError("network disabled in isolated tests")
            return original_connect_ex(stream, address)

        def no_dns(*args, **kwargs):
            raise OSError("DNS disabled in isolated tests")

        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex
        socket.getaddrinfo = no_dns
        import pytest

        addopts = "--strict-config --strict-markers"
        if options.coverage:
            addopts += " --cov=src --cov-branch --cov-report="
        result = pytest.main(
            ["-q", "-p", "no:cacheprovider", "-o", "addopts=" + addopts, *pytest_args]
        )
        if options.coverage:
            import coverage

            report = coverage.Coverage(data_file=os.environ["COVERAGE_FILE"])
            report.load()
            report.xml_report(outfile=str(repo / "coverage.xml"))
            total = report.report(include=["src/domain/*", "src/application/*"], file=io.StringIO())
            print(f"Architecture branch coverage: {total:.2f}% (required: 90%)")
            if total < 90:
                return 2
        return int(result)


if __name__ == "__main__":
    raise SystemExit(main())
