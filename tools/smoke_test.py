#!/usr/bin/env python3
"""Smoke-test a freshly built PerimetryEditor binary.

Launches the bundled executable against a throwaway data dir, waits for it to
serve, and checks /health, /api/info (confirming it's the *frozen* build), and
the home page. Exits non-zero on any failure. Cross-platform (used by CI).

    python tools/smoke_test.py dist/PerimetryEditor        # macOS / Linux
    python tools/smoke_test.py dist/PerimetryEditor.exe    # Windows
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request


def main():
    if len(sys.argv) < 2:
        print("usage: smoke_test.py <path-to-binary>")
        sys.exit(2)
    binary = sys.argv[1]
    port = "8123"
    base = f"http://127.0.0.1:{port}"

    env = dict(os.environ)
    env["DATA_DIR"] = tempfile.mkdtemp(prefix="pe-smoke-")
    env["PORT"] = port
    env["PERIMETRY_NO_BROWSER"] = "1"

    proc = subprocess.Popen([binary], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        healthy = False
        for _ in range(60):                       # up to ~30 s (onefile unpacks slowly)
            if proc.poll() is not None:
                out = proc.stdout.read().decode("utf-8", "replace")
                print("Binary exited early:\n" + out)
                sys.exit(1)
            try:
                with urllib.request.urlopen(base + "/health", timeout=1) as r:
                    if r.read().strip() == b"ok":
                        healthy = True
                        break
            except Exception:
                time.sleep(0.5)
        if not healthy:
            print("Server never became healthy")
            sys.exit(1)

        with urllib.request.urlopen(base + "/api/info", timeout=3) as r:
            info = json.loads(r.read())
        assert info.get("frozen") is True, f"not a frozen build: {info}"
        assert info.get("version"), f"missing version: {info}"

        with urllib.request.urlopen(base + "/", timeout=3) as r:
            html = r.read()
        assert b"Perimetry Editor" in html, "home page missing expected content"

        print(f"SMOKE OK → {info}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
