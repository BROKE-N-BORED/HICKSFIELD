import unittest

from hicksfield import paths
from tests.helpers import TempEnvTestCase


class PathsTests(TempEnvTestCase):
    def test_data_dir_uses_override(self):
        self.assertEqual(paths.data_dir(), self.tmp / "data")

    def test_config_dir_uses_override(self):
        self.assertEqual(paths.config_dir(), self.tmp / "config")

    def test_projects_dir_uses_home_override(self):
        self.assertEqual(paths.projects_dir(), self.tmp / "home" / "HicksField")

    def test_subdirs_under_data(self):
        self.assertEqual(paths.log_dir(), self.tmp / "data" / "logs")
        self.assertEqual(paths.models_dir(), self.tmp / "data" / "models")
        self.assertEqual(paths.engines_dir(), self.tmp / "data" / "engines")
        self.assertEqual(paths.voices_dir(),
                         self.tmp / "data" / "models" / "voices")

    def test_ensure_dirs_creates_all(self):
        made = paths.ensure_dirs()
        for key, folder in made.items():
            self.assertTrue(folder.is_dir(), key)


if __name__ == "__main__":
    unittest.main()
