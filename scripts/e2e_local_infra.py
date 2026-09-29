#!/usr/bin/env python3
"""Isolated Redis and S3-compatible HTTP test fixture; no production credentials.

S3 signing authorization is deliberately not emulated; use only loopback disposable data.
Only the container ID returned by this process is stopped by cleanup.
"""

import argparse
import base64
import hashlib
import json
import os
import signal
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    root.chmod(0o700)
    store = root / "objects"
    store.mkdir()
    lock = threading.Lock()
    records = {}
    counts = {"PUT": 0, "HEAD": 0, "GET": 0, "DELETE": 0}
    done = threading.Event()
    container = None
    server = None
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    signal.signal(signal.SIGINT, lambda *_: done.set())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def key(self):
            return unquote(urlsplit(self.path).path)

        def headers_for(self, record):
            self.send_header("Content-Type", record["content_type"])
            self.send_header("ETag", '"' + record["etag"] + '"')
            self.send_header("x-amz-checksum-sha256", record["sha256_base64"])
            self.send_header("x-amz-meta-sha256", record["sha256"])
            self.send_header("Content-Length", str(record["size"]))
            self.send_header("Accept-Ranges", "bytes")

        def do_PUT(self):
            key = self.key()
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                chunks = []
                while True:
                    size = int(self.rfile.readline().split(b";")[0].strip(), 16)
                    if not size:
                        self.rfile.readline()
                        break
                    chunks.append(self.rfile.read(size))
                    self.rfile.read(2)
                body = b"".join(chunks)
            else:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            sha = hashlib.sha256(body).digest()
            file = store / hashlib.sha256(key.encode()).hexdigest()
            file.write_bytes(body)
            record = {
                "size": len(body),
                "etag": hashlib.md5(body).hexdigest(),
                "sha256": sha.hex(),
                "sha256_base64": base64.b64encode(sha).decode(),
                "file": str(file),
                "content_type": self.headers.get("Content-Type", "application/octet-stream"),
            }
            with lock:
                records[key] = record
                counts["PUT"] += 1
            self.send_response(200)
            self.send_header("ETag", '"' + record["etag"] + '"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_HEAD(self):
            with lock:
                record = records.get(self.key())
                counts["HEAD"] += 1
            if not record:
                self.send_error(404)
                return
            self.send_response(200)
            self.headers_for(record)
            self.end_headers()

        def do_GET(self):
            if self.key() == "/__fixture/stats":
                with lock:
                    payload = json.dumps(
                        {
                            "counts": counts,
                            "objects": len(records),
                            "bytes": sum(x["size"] for x in records.values()),
                        }
                    ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            with lock:
                record = records.get(self.key())
                counts["GET"] += 1
            if not record:
                self.send_error(404)
                return
            self.send_response(200)
            self.headers_for(record)
            self.end_headers()
            self.wfile.write(Path(record["file"]).read_bytes())

        def do_DELETE(self):
            with lock:
                record = records.pop(self.key(), None)
                counts["DELETE"] += 1
            if record:
                Path(record["file"]).unlink(missing_ok=True)
            self.send_response(204)
            self.end_headers()

    try:
        name = "gn-e2e-redis-" + uuid.uuid4().hex[:10]
        container = subprocess.check_output(
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--pull=never",
                "--name",
                name,
                "--label",
                "grn.e2e.disposable=true",
                "-p",
                "127.0.0.1::6379",
                "redis:7-alpine",
                "redis-server",
                "--save",
                "",
                "--appendonly",
                "no",
            ],
            text=True,
        ).strip()
        mapping = json.loads(
            subprocess.check_output(
                ["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", container],
                text=True,
            )
        )
        redis_port = int(mapping["6379/tcp"][0]["HostPort"])
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        info = {
            "pid": os.getpid(),
            "redis_container": container,
            "redis_port": redis_port,
            "s3_endpoint": f"http://127.0.0.1:{server.server_port}",
            "s3_bucket": "gn-e2e-local",
            "s3_access_key": "synthetic",
            "s3_secret_key": "synthetic-not-production",
        }
        (root / "infra.json").write_text(json.dumps(info, indent=2))
        print(json.dumps(info), flush=True)
        done.wait()
    finally:
        cleanup_errors = []
        try:
            if server:
                server.shutdown()
                server.server_close()
        except OSError as error:
            cleanup_errors.append({"resource": "storage", "type": type(error).__name__})
        if container:
            try:
                subprocess.run(
                    ["docker", "stop", "--time", "3", container],
                    stdout=subprocess.DEVNULL,
                    check=True,
                    timeout=15,
                )
            except (OSError, subprocess.SubprocessError) as error:
                cleanup_errors.append({"resource": "redis", "type": type(error).__name__})
        (root / "stats.json").write_text(
            json.dumps(
                {"counts": counts, "objects": len(records), "cleanup_errors": cleanup_errors},
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
