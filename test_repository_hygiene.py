import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class RepositoryHygieneTests(unittest.TestCase):
    def test_local_settings_template_contains_no_credentials(self):
        document = json.loads((ROOT / "local_settings.example.json").read_text(encoding="utf-8"))
        self.assertEqual(document.get("base_url"), "https://your-api-host.example")
        serialized = json.dumps(document)
        self.assertNotRegex(serialized, re.compile(r"sk-[A-Za-z0-9_-]{12,}"))
        self.assertNotRegex(serialized, re.compile(r"(access[_-]?key|secret|api[_-]?key)\s*[:=]\s*[^\"']+", re.I))

    def test_bootstrap_and_launchers_use_pinned_bootstrap(self):
        bootstrap = (ROOT / "bootstrap.ps1").read_text(encoding="utf-8-sig")
        launcher = (ROOT / "启动程序.bat").read_text(encoding="utf-8")
        installer = (ROOT / "安装依赖.bat").read_text(encoding="utf-8")
        self.assertIn("requirements.lock.txt", bootstrap)
        self.assertIn("-Mode Ensure", launcher)
        self.assertIn("bootstrap.ps1", installer)


if __name__ == "__main__":
    unittest.main()
