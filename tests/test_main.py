import subprocess
import sys
import unittest
from pathlib import Path

TREE = Path(__file__).resolve().parent.parent


class MainTests(unittest.TestCase):
    def test_version_without_qt(self):
        proc = subprocess.run(
            [sys.executable, "-m", "hicksfield", "--version"],
            cwd=str(TREE), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("HicksField 0.6.0", proc.stdout)

    def test_help(self):
        proc = subprocess.run(
            [sys.executable, "-m", "hicksfield", "--help"],
            cwd=str(TREE), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Usage:", proc.stdout)


if __name__ == "__main__":
    unittest.main()
