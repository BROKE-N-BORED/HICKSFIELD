import unittest

from hicksfield import scenes as scenes_mod
from hicksfield.errors import HicksFieldError
from tests.helpers import TempEnvTestCase


class ScenesTests(TempEnvTestCase):
    def test_save_load_round_trip(self):
        project = scenes_mod.Project(
            name="Test film", idea="A stool.",
            scenes=[scenes_mod.Scene(prompt="a stool",
                                     narration="A stool sits here.")])
        target = self.tmp / "film"
        scenes_mod.save_project(project, target)
        loaded = scenes_mod.load_project(target)
        self.assertEqual(loaded.name, "Test film")
        self.assertEqual(loaded.idea, "A stool.")
        self.assertEqual(len(loaded.scenes), 1)
        self.assertEqual(loaded.scenes[0].prompt, "a stool")
        self.assertTrue(loaded.created)
        self.assertTrue(loaded.updated)

    def test_asset_dirs_created(self):
        paths = scenes_mod.ensure_project_dirs(self.tmp / "film")
        for key in ("stills", "clips", "audio"):
            self.assertTrue(paths[key].is_dir(), key)
        self.assertEqual(paths["film"], self.tmp / "film" / "film.mp4")
        self.assertEqual(paths["captions"], self.tmp / "film" / "film.srt")

    def test_scene_file_names(self):
        self.assertEqual(scenes_mod.scene_still_name(0), "scene-01.png")
        self.assertEqual(scenes_mod.scene_clip_name(9), "scene-10.mp4")
        self.assertEqual(scenes_mod.scene_audio_name(2), "scene-03.wav")

    def test_load_missing_file_is_plain_error(self):
        with self.assertRaises(HicksFieldError):
            scenes_mod.load_project(self.tmp / "nope")

    def test_load_bad_json_is_plain_error(self):
        target = self.tmp / "film"
        target.mkdir()
        (target / "project.json").write_text("{not json",
                                             encoding="utf-8")
        with self.assertRaises(HicksFieldError):
            scenes_mod.load_project(target)

    def test_scene_from_dict_tolerates_junk(self):
        scene = scenes_mod.Scene.from_dict({"prompt": 5})
        self.assertEqual(scene.prompt, "5")
        self.assertEqual(scene.narration, "")
        self.assertIsInstance(scenes_mod.Scene.from_dict("junk"),
                              scenes_mod.Scene)

    def test_relative_paths_survive_a_move(self):
        project = scenes_mod.Project(
            scenes=[scenes_mod.Scene(prompt="p", narration="n",
                                     still="stills/scene-01.png")])
        first = self.tmp / "first"
        scenes_mod.save_project(project, first)
        moved = self.tmp / "moved"
        import shutil
        shutil.move(str(first), str(moved))
        loaded = scenes_mod.load_project(moved)
        self.assertEqual(loaded.scenes[0].still, "stills/scene-01.png")


if __name__ == "__main__":
    unittest.main()
