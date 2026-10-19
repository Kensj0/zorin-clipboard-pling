import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import wave
from unittest import mock

os.environ["CLIPBOARD_STATE"] = tempfile.mkdtemp(prefix="zorin-clipboard-test-")

import server  # noqa: E402


class Classify(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(server.classify(""), "empty")

    def test_plain_text(self):
        self.assertEqual(server.classify("hej världen"), "text")

    def test_url(self):
        self.assertEqual(server.classify("  https://example.se/a?b=1  "), "url")

    def test_json(self):
        self.assertEqual(server.classify('{"a": 1}'), "json")

    def test_broken_json_falls_back_to_text(self):
        self.assertEqual(server.classify("{inte json"), "text")

    def test_claude_handoff(self):
        self.assertEqual(server.classify(server.CLAUDE_SESSION_MARKER + "\nrest"), "claude")


class FakeReader:
    def __init__(self, *values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0)


class ClipboardState(unittest.TestCase):
    def test_startup_text_does_not_ring_and_changes_do(self):
        rings = []
        clipboard = server.Clipboard(FakeReader("a", "a", "b", None), lambda: rings.append(1))
        clipboard.poll()  # startup reading
        self.assertEqual(rings, [])
        self.assertFalse(clipboard.poll())  # same text again
        self.assertTrue(clipboard.poll())  # new text
        self.assertEqual(rings, [1])
        self.assertFalse(clipboard.poll())  # no clipboard tool answer
        self.assertEqual(clipboard.summary()["seq"], 2)

    def test_summary_never_contains_the_text(self):
        secret = "exempeltext som inte läcker"
        clipboard = server.Clipboard(FakeReader(secret), lambda: None)
        clipboard.poll()
        summary = clipboard.summary()
        self.assertEqual(summary["kind"], "text")
        self.assertEqual(summary["bytes"], len(secret.encode()))
        self.assertEqual(summary["lines"], 1)
        self.assertNotIn(secret, json.dumps(summary))
        self.assertEqual(clipboard.text(), secret)

    def test_lines_and_bytes_for_multiline_utf8(self):
        clipboard = server.Clipboard(FakeReader("å\nö\n"), lambda: None)
        clipboard.poll()
        self.assertEqual(clipboard.summary()["lines"], 3)
        self.assertEqual(clipboard.summary()["bytes"], len("å\nö\n".encode()))


class PlingDebounce(unittest.TestCase):
    def test_one_pling_per_window(self):
        played = []
        now = [0.0]
        pling = server.Pling(True, lambda: played.append(1), clock=lambda: now[0], debounce_s=0.3)
        pling()
        pling()  # within the window
        now[0] = 0.5
        pling()
        for thread in threading.enumerate():
            if thread is not threading.current_thread() and thread.daemon:
                thread.join(timeout=1)
        self.assertEqual(len(played), 2)

    def test_disabled_never_plays(self):
        played = []
        pling = server.Pling(False, lambda: played.append(1))
        pling()
        self.assertEqual(played, [])


class PlingSound(unittest.TestCase):
    def test_synthesized_pling_is_a_valid_short_wav(self):
        path = server.pling_wav()
        with wave.open(path, "rb") as file:
            self.assertEqual(file.getnchannels(), 1)
            self.assertEqual(file.getsampwidth(), 2)
            duration = file.getnframes() / file.getframerate()
        self.assertAlmostEqual(duration, 0.45, places=2)


class HttpApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.played = threading.Event()
        cls.clipboard = server.Clipboard(FakeReader("hemlig-text", "hemlig-text"), lambda: None)
        cls.clipboard.poll()
        app = {
            "clipboard": cls.clipboard,
            "watching": lambda: True,
            "play": cls.played.set,
            "zorin_status": lambda: {"available": False, "devices": []},
        }
        cls.httpd = server.make_server(0, app)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def request(self, method, path, body=None, host=None, origin=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        request.add_header("Host", host or f"127.0.0.1:{self.port}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if origin:
            request.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(request, timeout=5) as reply:
                raw = reply.read()
                return reply.status, json.loads(raw) if raw else None, raw
        except urllib.error.HTTPError as error:
            raw = error.read()
            return error.code, json.loads(raw) if raw else None, raw

    def test_clipboard_summary_has_no_text(self):
        status, body, raw = self.request("GET", "/clipboard")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"kind", "bytes", "lines", "copiedAt", "seq"})
        self.assertNotIn(b"hemlig-text", raw)

    def test_status_reports_zorin_state(self):
        status, body, _ = self.request("GET", "/status")
        self.assertEqual(status, 200)
        self.assertFalse(body["zorin"]["available"])
        self.assertIn("pling", body)
        self.assertIn("autopush", body)

    def test_pling_endpoint_plays_once(self):
        self.played.clear()
        status, _, _ = self.request("POST", "/pling")
        self.assertEqual(status, 204)
        self.assertTrue(self.played.wait(timeout=2))

    def test_unknown_host_is_forbidden(self):
        status, _, _ = self.request("GET", "/clipboard", host="evil.example")
        self.assertEqual(status, 403)

    def test_foreign_origin_is_forbidden(self):
        status, _, _ = self.request("GET", "/clipboard", origin="https://evil.example")
        self.assertEqual(status, 403)

    def test_push_needs_a_device_id(self):
        status, body, _ = self.request("POST", "/push", body={})
        self.assertEqual(status, 400)

    def test_push_rejects_a_bad_device_id(self):
        status, body, _ = self.request("POST", "/push", body={"deviceId": "../etc"})
        self.assertEqual(status, 400)

    def test_push_reports_zorin_unreachable_as_503(self):
        with mock.patch.object(server, "zorin_send", side_effect=server.ZorinUnavailable("no bus")):
            status, body, _ = self.request("POST", "/push", body={"deviceId": "abc123"})
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "zorin connect not reachable")

    def test_push_and_pull_call_zorin_with_the_right_action(self):
        with mock.patch.object(server, "zorin_send") as send:
            self.assertEqual(self.request("POST", "/push", body={"deviceId": "abc123"})[0], 204)
            self.assertEqual(self.request("POST", "/pull", body={"deviceId": "abc123"})[0], 204)
        send.assert_any_call("abc123", "clipboardPush")
        send.assert_any_call("abc123", "clipboardPull")


if __name__ == "__main__":
    unittest.main()
