"""Synthetic native-status executable; never reads real authentication stores."""

import json
import os
import signal
import sys
import time
from pathlib import Path

if sys.argv[1:] not in (["auth", "status"], ["login", "status"]):
    sys.exit(9)
mode = os.environ.get("AUTH_PROBE_FIXTURE_MODE", "success")
if marker := os.environ.get("AUTH_PROBE_FIXTURE_PID"):
    Path(marker).write_text(str(os.getpid()))
if mode == "environment":
    keys = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "MODEL_GATEWAY")
    print(json.dumps({"forbidden_present": any(key in os.environ for key in keys)}))
elif mode == "logged-out":
    print('{"loggedIn":false}')
elif mode == "bad-json":
    print("not authenticated")
elif mode == "array":
    print("[]")
elif mode == "string-true":
    print('{"loggedIn":"true"}')
elif mode == "conflict":
    print('{"loggedIn":true,"authenticated":false}')
elif mode == "authenticated":
    print('{"authenticated":true}')
elif mode == "invalid-utf8":
    os.write(1, b"\xff\xfe\xff")
elif mode == "nonzero":
    print('{"loggedIn":true}')
    sys.exit(2)
elif mode == "overflow":
    while True:
        os.write(1, b"x" * 8192)
elif mode == "stderr":
    os.write(2, b"synthetic-auth-output-not-for-evidence" * 8192)
    print('{"loggedIn":true}')
elif mode in ("sleep", "term-resistant", "early-stdout-close"):
    if mode == "term-resistant":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if mode == "early-stdout-close":
        os.close(1)
    time.sleep(60)
elif mode in ("child-held-pipe", "child-detached-output"):
    child = os.fork()
    if child == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if mode == "child-detached-output":
            os.close(1)
        time.sleep(60)
        os._exit(0)
    Path(os.environ["AUTH_PROBE_FIXTURE_CHILD_PID"]).write_text(str(child))
    print('{"loggedIn":true}', flush=True)
else:
    print('{"loggedIn":true}')
