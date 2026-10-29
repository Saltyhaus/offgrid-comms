"""Systems tab: radio-profile output parsing and saved-setup state."""
import json, os, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class SystemsPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["XDG_DATA_HOME"] = self.tmp.name
        import importlib, systems_page
        self.sp = importlib.reload(systems_page)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_plan(self):
        out = "reading the KX3…\n2 change(s) to 'voice':\n  mode   DATA A  →  LSB\n  MG  mic gain  012  →  040\n(dry run -- nothing written)\n"
        self.assertEqual(self.sp.parse_plan(out), ["mode DATA A → LSB", "MG mic gain 012 → 040"])
        self.assertEqual(self.sp.parse_plan("already matches 'data' -- nothing to change"), [])
        self.assertIsNone(self.sp.parse_plan("CAT: can't reach rigctld"))

    def test_profile_state(self):
        d = Path(self.tmp.name) / "radio-profile" / "kx3"
        d.mkdir(parents=True)
        self.assertEqual(self.sp.profile_state("kx3"), ([], None, None))
        (d / "data.json").write_text("{}")
        (d / "current.json").write_text(json.dumps({"name": "data", "time": "2026-10-25T17:02:00"}))
        self.assertEqual(self.sp.profile_state("kx3"), (["data"], "data", "2026-10-25T17:02:00"))

    def test_bundled_tool_has_both_radios(self):
        import radio_profile
        self.assertEqual(set(radio_profile.RADIOS), {"ft991a", "kx3"})
        self.assertEqual(set(self.sp.PROFILE_IDS.values()), {"ft991a", "kx3"})


if __name__ == "__main__":
    unittest.main()
