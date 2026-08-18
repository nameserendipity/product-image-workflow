import json
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class BootstrapContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = (ROOT / "bootstrap.ps1").read_text(encoding="utf-8")

    def test_declares_modes_and_testable_root(self) -> None:
        self.assertIn('[ValidateSet("Ensure", "Check")]' , self.script)
        self.assertIn('[switch]$NonInteractive', self.script)
        self.assertIn('[string]$Root = $PSScriptRoot', self.script)

    def test_references_pinned_inputs_and_hash_verification(self) -> None:
        self.assertIn('runtime-versions.json', self.script)
        self.assertIn('requirements.lock.txt', self.script)
        self.assertIn('Get-FileHash -Algorithm SHA256', self.script)
        self.assertIn('"-m", "pip", "install"', self.script)
        self.assertIn('"-m", "playwright", "install", "chromium"', self.script)

    def test_initializes_config_and_state(self) -> None:
        self.assertIn('local_settings.example.json', self.script)
        self.assertIn('Copy-Item -LiteralPath $TemplatePath -Destination $SettingsPath', self.script)
        self.assertIn('bootstrap-state.json', self.script)
        self.assertIn('runtime_manifest_sha256', self.script)
        self.assertIn('$manifestHash = Get-Sha256 $ManifestPath', self.script)

    def test_creates_runtime_directory_before_chromium_marker(self) -> None:
        function = re.search(
            r"function Ensure-Chromium.*?\n}\n",
            self.script,
            re.DOTALL,
        )
        self.assertIsNotNone(function)
        body = function.group(0)
        create_index = body.index("New-Item -ItemType Directory -Force -Path $RuntimeRoot")
        marker_index = body.index('Set-Content -LiteralPath $marker')
        self.assertLess(create_index, marker_index)

    def test_quotes_python_runtime_install_target(self) -> None:
        self.assertIn('"TargetDir=`"$target`""', self.script)
        self.assertNotIn('"TargetDir=$target"', self.script)

    @unittest.skipUnless(__import__("platform").system() == "Windows", "PowerShell contract smoke test is Windows-only")
    def test_check_mode_is_non_interactive(self) -> None:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "bootstrap.ps1"), "-Mode", "Check", "-NonInteractive"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertIn("bootstrap", ((result.stdout or "") + (result.stderr or "")).lower())


if __name__ == "__main__":
    unittest.main()
