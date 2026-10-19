"""Clipboard pling: watches the clipboard, plays a short pling on each change, and bridges to Zorin Connect.

Runs on its own with `python3 server.py`. Any client can use it, a panel in Jarri is optional.
Zorin Connect keeps doing the phone sync itself. This service only asks the Zorin service that is
already running to push or pull the clipboard for a device.

The classification rule follows ClipboardBytes by TorMatzAndren (github.com/TorMatzAndren/ClipboardBytes).
No code from that project is used.

  GET  /clipboard          -> {kind, bytes, lines, copiedAt, seq}  (never the text itself)
  GET  /status             -> {session, watching, pling, autopush, zorin: {available, devices}}
  POST /pling              -> 204  (plays the pling once, to test the sound)
  POST /push {deviceId}    -> 204 | {error}  (Zorin sends the desktop clipboard to the phone)
  POST /pull {deviceId}    -> 204 | {error}  (Zorin sends the phone clipboard to the desktop)

Settings, all optional: CLIPBOARD_PORT (8787), CLIPBOARD_PLING (1), CLIPBOARD_AUTOPUSH (0),
CLIPBOARD_POLL_S (0.5). Binds to 127.0.0.1 only.
"""

import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HOST = "127.0.0.1"
PORT = int(os.environ.get("CLIPBOARD_PORT", "8787"))
PLING_ON = os.environ.get("CLIPBOARD_PLING", "1") == "1"
AUTOPUSH_ON = os.environ.get("CLIPBOARD_AUTOPUSH", "0") == "1"
POLL_S = float(os.environ.get("CLIPBOARD_POLL_S", "0.5"))
STATE_DIR = os.path.expanduser(os.environ.get("CLIPBOARD_STATE", "~/.local/state/zorin-clipboard-pling"))
PLING_DEBOUNCE_S = 0.3
CALL_TIMEOUT_MS = 3000
ZORIN_SERVICE = "org.gnome.Shell.Extensions.ZorinConnect"
ZORIN_ROOT = "/org/gnome/Shell/Extensions/ZorinConnect"
ZORIN_DEVICE = "org.gnome.Shell.Extensions.ZorinConnect.Device"
ALLOWED_ORIGINS = {
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "http://localhost:8792",
    "http://127.0.0.1:8792",
}
DEVICE_ID = re.compile(r"^[A-Za-z0-9]+$")
CLAUDE_SESSION_MARKER = "===== CLAUDE CODE SESSION ====="
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)


def classify(text):
    """Empty, claude handoff, url, json or text. Only classify when the structure is certain."""
    if text == "":
        return "empty"
    if text.startswith(CLAUDE_SESSION_MARKER):
        return "claude"
    trimmed = text.strip()
    if URL_PATTERN.fullmatch(trimmed):
        return "url"
    if trimmed.startswith("{") or trimmed.startswith("["):
        try:
            json.loads(trimmed)
            return "json"
        except ValueError:
            pass
    return "text"


def clipboard_command():
    """The command that prints the clipboard, or None when no clipboard tool works here."""
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        return ["wl-paste", "--no-newline"]
    if os.environ.get("DISPLAY") and shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard", "-o"]
    if os.environ.get("DISPLAY") and shutil.which("xsel"):
        return ["xsel", "--clipboard", "--output"]
    return None


def read_clipboard():
    """The clipboard text, or None when no clipboard tool works here."""
    command = clipboard_command()
    if command is None:
        return None
    try:
        result = subprocess.run(command, capture_output=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return ""  # xclip and wl-paste exit non-zero when the clipboard is empty
    return result.stdout.decode("utf-8", errors="replace")


class Clipboard:
    """Remembers the last clipboard text. Only the summary leaves this process; the text stays here."""

    def __init__(self, read, on_change):
        self._read = read
        self._on_change = on_change
        self._lock = threading.Lock()
        self._digest = None
        self._text = ""
        self.seq = 0
        self.copied_at = None
        self.kind = "empty"
        self.bytes = 0
        self.lines = 0

    def poll(self):
        text = self._read()
        if text is None:
            return False
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._lock:
            if digest == self._digest:
                return False
            first_reading = self._digest is None
            self._digest = digest
            self._text = text
            self.seq += 1
            self.copied_at = int(time.time() * 1000)
            self.kind = classify(text)
            self.bytes = len(text.encode("utf-8"))
            self.lines = 0 if text == "" else text.count("\n") + 1
        if not first_reading:  # the text already on the clipboard at startup does not ring
            self._on_change()
        return True

    def text(self):
        with self._lock:
            return self._text

    def summary(self):
        with self._lock:
            return {
                "kind": self.kind,
                "bytes": self.bytes,
                "lines": self.lines,
                "copiedAt": self.copied_at,
                "seq": self.seq,
            }


def pling_wav():
    """A short synthesized pling, the same sweep as the Jarri shell (1320 to 1760 Hz, 0.45 s)."""
    path = os.path.join(STATE_DIR, "pling.wav")
    if os.path.isfile(path):
        return path
    os.makedirs(STATE_DIR, exist_ok=True)
    rate = 22050
    frames = []
    phase = 0.0
    total = int(rate * 0.45)
    for i in range(total):
        t = i / rate
        frequency = 1320 * (1760 / 1320) ** min(1.0, t / 0.08)
        phase += 2 * math.pi * frequency / rate
        if t < 0.01:
            envelope = 0.18 * (t / 0.01)
        else:
            envelope = 0.18 * (0.0001 / 0.18) ** ((t - 0.01) / 0.44)
        frames.append(struct.pack("<h", int(32767 * envelope * math.sin(phase))))
    with wave.open(path, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(rate)
        file.writeframes(b"".join(frames))
    return path


def play_sound():
    """Plays the same pling as the Jarri shell used to. Always this one, never the system sound theme."""
    if not shutil.which("canberra-gtk-play"):
        return
    subprocess.run(["canberra-gtk-play", "-f", pling_wav()], capture_output=True, timeout=10)


class Pling:
    """Calls play() at most once per debounce window, on a thread so polling never waits for sound."""

    def __init__(self, enabled, play, clock=time.monotonic, debounce_s=PLING_DEBOUNCE_S):
        self._enabled = enabled
        self._play = play
        self._clock = clock
        self._debounce_s = debounce_s
        self._last = None

    def __call__(self):
        if not self._enabled:
            return
        now = self._clock()
        if self._last is not None and now - self._last < self._debounce_s:
            return
        self._last = now
        threading.Thread(target=self._play, daemon=True).start()


class ZorinUnavailable(Exception):
    pass


def zorin_call(path, interface, method, params, reply_type):
    try:
        from gi.repository import Gio, GLib
    except ImportError as error:
        raise ZorinUnavailable(str(error))
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return bus.call_sync(
            ZORIN_SERVICE, path, interface, method, params,
            GLib.VariantType.new(reply_type), Gio.DBusCallFlags.NONE,
            CALL_TIMEOUT_MS, None,
        )
    except GLib.Error as error:
        raise ZorinUnavailable(str(error))


def zorin_status():
    try:
        managed = zorin_call(ZORIN_ROOT, "org.freedesktop.DBus.ObjectManager",
                             "GetManagedObjects", None, "(a{oa{sa{sv}}})").unpack()[0]
    except ZorinUnavailable:
        return {"available": False, "devices": []}
    devices = []
    for path, interfaces in managed.items():
        props = interfaces.get(ZORIN_DEVICE)
        if props is None:
            continue
        devices.append({
            "id": path.rsplit("/", 1)[-1],
            "name": str(props.get("Name", "")),
            "connected": bool(props.get("Connected", False)),
            "paired": bool(props.get("Paired", False)),
        })
    devices.sort(key=lambda d: d["name"])
    return {"available": True, "devices": devices}


def zorin_send(device_id, action):
    """Asks Zorin to run 'clipboardPush' or 'clipboardPull' for one device."""
    if not DEVICE_ID.match(device_id):
        raise ValueError("invalid device id")
    try:
        from gi.repository import GLib
    except ImportError as error:
        raise ZorinUnavailable(str(error))
    params = GLib.Variant.parse(GLib.VariantType.new("(sava{sv})"), f"('{action}', [], {{}})")
    zorin_call(f"{ZORIN_ROOT}/Device/{device_id}", "org.gtk.Actions", "Activate", params, "()")


def autopush(devices):
    """Sends the desktop clipboard to every connected device. Off by default: Zorin already syncs."""
    for device in devices:
        if device["connected"]:
            try:
                zorin_send(device["id"], "clipboardPush")
            except (ZorinUnavailable, ValueError):
                pass


def session_type():
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "none"


class Handler(BaseHTTPRequestHandler):
    server_version = "zorin-clipboard-pling"

    def log_message(self, fmt, *args):
        pass

    def _allowed(self):
        origin = self.headers.get("Origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            return False
        return self.headers.get("Host") in self.server.allowed_hosts

    def _send(self, status_code, body=None):
        self.send_response(status_code)
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        if body is None:
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        raw = json.dumps(body).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 4096:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return None

    def do_OPTIONS(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden origin"})
        self._send(204)

    def do_GET(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden origin"})
        path = urlparse(self.path).path
        app = self.server.app
        if path == "/clipboard":
            return self._send(200, app["clipboard"].summary())
        if path == "/status":
            return self._send(200, {
                "session": session_type(),
                "watching": app["watching"](),
                "pling": PLING_ON,
                "autopush": AUTOPUSH_ON,
                "zorin": app["zorin_status"](),
            })
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden origin"})
        path = urlparse(self.path).path
        app = self.server.app
        if path == "/pling":
            threading.Thread(target=app["play"], daemon=True).start()
            return self._send(204)
        if path in ("/push", "/pull"):
            body = self._body()
            if body is None or not isinstance(body.get("deviceId"), str):
                return self._send(400, {"error": "deviceId is required"})
            action = "clipboardPush" if path == "/push" else "clipboardPull"
            try:
                zorin_send(body["deviceId"], action)
            except ValueError as error:
                return self._send(400, {"error": str(error)})
            except ZorinUnavailable as error:
                return self._send(503, {"error": "zorin connect not reachable", "detail": str(error)})
            return self._send(204)
        return self._send(404, {"error": "not found"})


def make_server(port, app):
    server = ThreadingHTTPServer((HOST, port), Handler)
    server.app = app
    server.allowed_hosts = {f"{HOST}:{server.server_address[1]}", f"localhost:{server.server_address[1]}"}
    return server


def watch(clipboard, stop):
    while not stop.is_set():
        clipboard.poll()
        stop.wait(POLL_S)


def main():
    stop = threading.Event()
    pling = Pling(PLING_ON, play_sound)

    def on_change():
        pling()
        if AUTOPUSH_ON:
            autopush(zorin_status()["devices"])

    clipboard = Clipboard(read_clipboard, on_change)
    app = {
        "clipboard": clipboard,
        "watching": lambda: clipboard_command() is not None,
        "play": play_sound,
        "zorin_status": zorin_status,
    }
    threading.Thread(target=watch, args=(clipboard, stop), daemon=True).start()
    server = make_server(PORT, app)
    print(f"zorin clipboard pling on http://{HOST}:{PORT}", file=sys.stderr)
    try:
        server.serve_forever()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
