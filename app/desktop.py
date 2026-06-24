#!/usr/bin/env python3
"""Desktop launcher for the Perimetry Editor.

This is the entry point bundled by PyInstaller into a single, double-click
executable (no Python install required). On launch it:

  * keeps your data in a stable, easy-to-find folder (``~/PerimetryEditor``),
  * picks a free local port and binds to localhost only (not the network),
  * starts the editor and opens it in your default web browser,
  * stays running in this window — close the window to quit.

Run from source with:  python3 app/desktop.py
Override the data folder with the DATA_DIR environment variable.
"""
import os
import socket
import sys
import threading
import webbrowser


def default_data_dir():
    """A stable, easy-to-find folder in the user's home directory."""
    return os.path.join(os.path.expanduser("~"), "PerimetryEditor")


def pick_port(preferred=8766):
    """Return a bindable localhost port: ``preferred`` if free, else any free one."""
    for port in (preferred, 0):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
        except OSError:
            continue
    return preferred


def main():
    # Windows consoles often default to cp1252; make sure printing the banner
    # (or a data path with non-ASCII characters) can never crash the app.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # Set the data folder BEFORE importing the server, which reads it at import.
    os.environ.setdefault("DATA_DIR", default_data_dir())

    host = "127.0.0.1"
    port = int(os.environ["PORT"]) if os.environ.get("PORT") else pick_port()

    # The launcher lives next to server.py / hvf_24_2.py inside the bundle.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import server  # noqa: E402

    server._ensure_dirs()
    server.load_persisted()
    server.discover_subjects()

    url = f"http://{host}:{port}/"
    httpd = server.build_server(host, port)

    print(
        "\n"
        "  ==================================================\n"
        "    Perimetry Editor - HFA 24-2 Total Deviation\n"
        "  ==================================================\n"
        f"  Open in browser : {url}\n"
        f"  Your data folder: {os.environ['DATA_DIR']}\n"
        "  (uploaded reports + edits are saved here: images/ and extracted/)\n"
        "\n"
        "  Leave this window open while you work. Close it to quit.\n",
        flush=True,
    )

    # Open the browser shortly after the server starts accepting connections.
    # PERIMETRY_NO_BROWSER lets headless smoke tests skip this.
    if not os.environ.get("PERIMETRY_NO_BROWSER"):
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        print("\n  Stopped. You can close this window.")


if __name__ == "__main__":
    main()
