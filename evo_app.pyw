"""EVO desktop application: local server + native window."""
import os
import socket
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
sys.path.insert(0, BASE)

import webview  # noqa: E402


def _wait_port(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def serve() -> None:
    import uvicorn

    from core.config import HOST, PORT
    from main import app

    host = "0.0.0.0" if os.environ.get("JARVIS_LAN") == "1" else HOST
    uvicorn.run(app, host=host, port=PORT, log_level="warning")


def main() -> None:
    threading.Thread(target=serve, daemon=True).start()

    from core.config import ASSISTANT_NAME, PORT

    if not _wait_port(PORT):
        print("EVO server failed to start.", file=sys.stderr)

    webview.create_window(
        f"{ASSISTANT_NAME} — Console",
        f"http://127.0.0.1:{PORT}",
        width=1340,
        height=880,
        min_size=(980, 640),
        background_color="#070b14",
    )
    webview.start()
    os._exit(0)


if __name__ == "__main__":
    main()
