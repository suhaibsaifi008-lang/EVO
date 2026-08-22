import os
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from core.config import DATA_DIR, ROOT  # noqa: E402
from core.config import ASSISTANT_NAME  # noqa: E402

SERVER_PORT = 8420


def serve() -> None:
    import uvicorn

    from main import app

    uvicorn.run(app, host="127.0.0.1", port=SERVER_PORT, log_level="warning", log_config=None)


def wait_port(timeout: float = 20.0) -> bool:
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", SERVER_PORT), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def make_icon_image():
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([4, 4, 60, 60], outline=(79, 210, 255, 255), width=4)
    d.ellipse([16, 16, 48, 48], outline=(79, 210, 255, 220), width=3)
    d.ellipse([26, 26, 38, 38], fill=(140, 230, 255, 255))
    return img


def main() -> None:
    threading.Thread(target=serve, daemon=True).start()

    try:
        import pystray
    except ImportError:
        if wait_port():
            webbrowser.open(f"http://localhost:{SERVER_PORT}")
        print("pystray not installed - run: pip install pystray pillow")
        threading.Event().wait()
        return

    opened = {"flag": False}

    def on_open(icon, item):
        webbrowser.open(f"http://localhost:{SERVER_PORT}")

    def on_quit(icon, item):
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Open Console", on_open, default=True),
        pystray.MenuItem("Quit JARVIS", on_quit),
    )
    icon = pystray.Icon("evo", make_icon_image(), f"{ASSISTANT_NAME} - personal assistant", menu)

    def opener():
        if wait_port() and not opened["flag"]:
            opened["flag"] = True
            webbrowser.open(f"http://localhost:{SERVER_PORT}")

    threading.Thread(target=opener, daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
