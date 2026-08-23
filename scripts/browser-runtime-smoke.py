#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory


class SmokeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/":
            self.send_error(404)
            return
        body = b"<!doctype html><title>Engineering Control</title><main>browser-ready</main>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_arguments: object) -> None:
        return


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run credential-free Playwright runtime checks")
    parser.add_argument("--browser-root", type=Path, default=Path("/opt/engineering-browser"))
    parser.add_argument("--temp-root", type=Path, default=Path("/srv/engineering/tmp/browser"))
    parser.add_argument("--node", type=Path, default=Path("/usr/local/bin/node"))
    parser.add_argument(
        "--browsers-root", type=Path, default=Path("/opt/engineering-browser/browsers")
    )
    return parser.parse_args()


def verify_result(output_root: Path, browser: str) -> None:
    result_path = output_root / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("status") != "PASS" or result.get("visual_evidence") != "CAPTURED":
        raise RuntimeError(f"{browser} journey did not return a clean structured result")
    for artifact_name in ("final.png", "trace.zip"):
        artifact = output_root / artifact_name
        if not artifact.is_file() or artifact.stat().st_size == 0:
            raise RuntimeError(f"{browser} did not produce {artifact_name}")


def main() -> None:
    arguments = parse_arguments()
    script = arguments.browser_root / "run-journey.mjs"
    if not script.is_file() or not arguments.node.is_file():
        raise RuntimeError("browser runner installation is incomplete")
    if not arguments.temp_root.is_dir():
        raise RuntimeError("browser temporary root is unavailable")

    server = ThreadingHTTPServer(("127.0.0.1", 0), SmokeHandler)
    thread = threading.Thread(target=server.serve_forever, name="browser-smoke-http", daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        with TemporaryDirectory(prefix="verify-worker.", dir=arguments.temp_root) as temporary:
            temporary_root = Path(temporary)
            for browser in ("chromium", "firefox"):
                plan_path = temporary_root / f"{browser}.json"
                output_root = temporary_root / browser
                plan_path.write_text(
                    json.dumps(
                        {
                            "start_url": f"{origin}/",
                            "allowed_origins": [origin],
                            "browser": browser,
                            "timeout_ms": 30000,
                            "capture_visual_evidence": True,
                            "steps": [
                                {"action": "wait-for-text", "text": "browser-ready", "exact": True}
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                environment = {
                    "HOME": str(Path.home()),
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "PLAYWRIGHT_BROWSERS_PATH": str(arguments.browsers_root),
                    "ENGINEERING_JOURNEY_PLAN": str(plan_path),
                    "ENGINEERING_JOURNEY_OUTPUT": str(output_root),
                }
                completed = subprocess.run(  # noqa: S603 - executable and arguments are fixed
                    [str(arguments.node), str(script)],
                    cwd=arguments.browser_root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                if completed.returncode != 0:
                    detail = (completed.stderr or completed.stdout).strip()[:500]
                    raise RuntimeError(f"{browser} runtime smoke failed: {detail}")
                verify_result(output_root, browser)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print("PASS browser runtime smoke chromium firefox")


if __name__ == "__main__":
    main()
