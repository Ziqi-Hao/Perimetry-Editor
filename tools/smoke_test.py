#!/usr/bin/env python3
"""Smoke-test a freshly built PerimetryEditor binary.

Launches the bundled executable against a throwaway data dir, waits for it to
serve, and checks /health, /api/info (confirming it's the *frozen* build), and
the home page. Exits non-zero on any failure, dumping the binary's own output
so CI logs show exactly what went wrong. Cross-platform (used by CI).

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

DEADLINE_SECONDS = 120   # generous: Windows onefile unpack + Defender scan is slow


def main():
    if len(sys.argv) < 2:
        print("usage: smoke_test.py <path-to-binary>")
        sys.exit(2)
    binary = sys.argv[1]
    port = "8123"
    base = f"http://127.0.0.1:{port}"

    dist_dir = os.path.dirname(binary) or "."
    print(f"binary: {binary}  exists: {os.path.exists(binary)}")
    try:
        print(f"{dist_dir}/ contents: {os.listdir(dist_dir)}")
    except OSError as e:
        print(f"(could not list {dist_dir}: {e})")

    data_dir = tempfile.mkdtemp(prefix="pe-smoke-")
    env = dict(os.environ)
    env["DATA_DIR"] = data_dir
    env["PORT"] = port
    env["PERIMETRY_NO_BROWSER"] = "1"

    # Capture the binary's stdout/stderr to a file (non-blocking to read later).
    log_path = os.path.join(data_dir, "_binary_output.txt")
    log_fh = open(log_path, "w+", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([binary], env=env, stdout=log_fh, stderr=subprocess.STDOUT)

    def shutdown():
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def fail(reason):
        print(f"SMOKE FAIL: {reason}")
        shutdown()
        log_fh.flush()
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                out = f.read()
        except OSError as e:
            out = f"(could not read captured output: {e})"
        print("----- binary output -----")
        print(out or "(no output captured)")
        print("-------------------------")
        sys.exit(1)

    deadline = time.time() + DEADLINE_SECONDS
    healthy = False
    while time.time() < deadline:
        if proc.poll() is not None:
            fail(f"binary exited early with code {proc.returncode}")
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if r.read().strip() == b"ok":
                    healthy = True
                    break
        except Exception:
            time.sleep(1)
    if not healthy:
        fail(f"server never became healthy within {DEADLINE_SECONDS}s")

    try:
        with urllib.request.urlopen(base + "/api/info", timeout=3) as r:
            info = json.loads(r.read())
        assert info.get("frozen") is True, f"not a frozen build: {info}"
        assert info.get("version"), f"missing version: {info}"
        with urllib.request.urlopen(base + "/", timeout=3) as r:
            html = r.read()
        assert b"Perimetry Editor" in html, "home page missing expected content"
    except Exception as e:
        fail(f"post-health check failed: {e}")

    print(f"SMOKE OK -> {info}")
    shutdown()


if __name__ == "__main__":
    main()
