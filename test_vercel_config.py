"""Guards the Vercel layout that makes every /api/* endpoint routable (no network calls).

With the Flask framework preset, Vercel builds a single function from api/index.py
and routes every /api/* request to it, so each other endpoint answers 404. vercel.json
pins the Other preset ("framework": null) to prevent that.
"""

import fnmatch
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# api_common.py loads notify, db, guard_core and remover_core with importlib
# (_optional_import), which Vercel's static import tracing cannot see. Every file
# listed here must therefore be shipped explicitly through functions.includeFiles.
BUNDLED_FILES = (
    "api_common.py",
    "notify.py",
    "db.py",
    "guard_core.py",
    "remover_core.py",
    "index.html",
)


class VercelConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))

    def test_framework_preset_is_other(self):
        # requirements.txt lists flask, which makes Vercel auto-select its Flask
        # preset. An explicit null keeps each api/*.py as its own endpoint.
        self.assertIn("framework", self.config)
        self.assertIsNone(self.config["framework"])

    def test_every_endpoint_module_defines_a_flask_app(self):
        modules = sorted((ROOT / "api").glob("*.py"))
        self.assertIn("bot_connect.py", [m.name for m in modules])
        for path in modules:
            with self.subTest(module=path.name):
                source = path.read_text(encoding="utf-8")
                self.assertRegex(source, r"(?m)^app = Flask\(")

    def test_functions_pattern_and_included_files(self):
        self.assertIn("api/*.py", self.config["functions"])
        spec = self.config["functions"]["api/*.py"]
        included = spec["includeFiles"].strip("{}").split(",")
        for name in BUNDLED_FILES:
            with self.subTest(file=name):
                self.assertIn(name, included)
                self.assertTrue((ROOT / name).is_file())

    def test_bundled_files_are_not_excluded_by_vercelignore(self):
        patterns = [
            line.strip()
            for line in (ROOT / ".vercelignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        for name in BUNDLED_FILES:
            with self.subTest(file=name):
                self.assertFalse(
                    any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)
                )


if __name__ == "__main__":
    unittest.main()
