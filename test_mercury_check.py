"""Mercury version check: this machine's binary vs home's published version."""
import http.server, json, os, sys, tempfile, threading, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mercury_check as mc


def fake(d, version=None):
    p = os.path.join(d, "mercury")
    with open(p, "w") as f:
        if version:
            f.write(f'#!/bin/sh\n[ "$1" = -V ] && printf "\\033[0;31mRhizomatica Mercury Version {version} '
                    f'(git edca701b)\\033[0m\\n"\n')
        else:
            f.write('#!/bin/sh\necho "Usage: mercury [-h] ..." >&2\nexit 255\n')
    os.chmod(p, 0o755)
    return p


class Serve:
    def __init__(self, doc):
        body = json.dumps(doc).encode()
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(body)
            def log_message(self, *a): pass
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/hf-contact.json"


class MercuryCheck(unittest.TestCase):
    def test_binary_version(self):
        self.assertEqual(mc.binary_version(fake(tempfile.mkdtemp(), "1.9.21")), ("ok", "1.9.21"))
        self.assertEqual(mc.binary_version(fake(tempfile.mkdtemp())), ("old", None))
        self.assertEqual(mc.binary_version("/nonexistent/mercury"), ("missing", None))
        self.assertEqual(mc.binary_version(""), ("missing", None))

    def test_home_version(self):
        s = Serve({"status": "listening", "mercury_version": "1.9.21"})
        self.assertEqual(mc.home_version(s.url), "1.9.21")
        self.assertIsNone(mc.home_version(Serve({"mercury_version": "1.9.21<b>"}).url))
        self.assertIsNone(mc.home_version(Serve({"status": "off"}).url))
        self.assertIsNone(mc.home_version("http://127.0.0.1:1/x.json", timeout=2))     # offline
        self.assertIsNone(mc.home_version(""))                                          # not set
        s.srv.shutdown()

    def test_summarize(self):
        self.assertEqual(mc.summarize(("ok", "1.9.21"), "1.9.21", "AG7JC"), ("green", "1.9.21 · AG7JC 1.9.21 ✓", False))
        light, text, mismatch = mc.summarize(("ok", "1.9.13"), "1.9.21", "AG7JC")
        self.assertEqual((light, mismatch), ("orange", True))
        self.assertIn("AG7JC runs 1.9.21", text)
        self.assertTrue(mc.summarize(("old", None), "1.9.21")[2])
        self.assertEqual(mc.summarize(("ok", "1.9.21"), None)[:1], ("green",))           # offline: no warning
        self.assertFalse(mc.summarize(("ok", "1.9.21"), None)[2])
        self.assertEqual(mc.summarize(("missing", None), "1.9.21"), ("gray", "Mercury not found", False))


if __name__ == "__main__":
    unittest.main()
