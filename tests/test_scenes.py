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

    def test_saved_project_has_version_1(self):
        import json
        project = scenes_mod.Project(name="Test film")
        target = self.tmp / "film"
        scenes_mod.save_project(project, target)
        data = json.loads(
            (target / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        loaded = scenes_mod.load_project(target)
        self.assertEqual(loaded.name, "Test film")

    def test_missing_version_loads_as_old_project(self):
        import json
        target = self.tmp / "film"
        target.mkdir()
        (target / "project.json").write_text(
            json.dumps({"name": "Old film", "idea": "", "scenes": []}),
            encoding="utf-8")
        loaded = scenes_mod.load_project(target)
        self.assertEqual(loaded.name, "Old film")

    def test_future_version_is_plain_error(self):
        import json
        target = self.tmp / "film"
        target.mkdir()
        (target / "project.json").write_text(
            json.dumps({"version": 99, "name": "New film", "scenes": []}),
            encoding="utf-8")
        with self.assertRaises(HicksFieldError) as ctx:
            scenes_mod.load_project(target)
        self.assertIn("newer HicksField", ctx.exception.plain_text())
        self.assertIn("Update HicksField", ctx.exception.plain_text())


class ProvenanceTests(TempEnvTestCase):
    def test_provenance_and_seed_round_trip(self):
        project = scenes_mod.Project(
            name="Test film",
            scenes=[scenes_mod.Scene(
                prompt="a stool", narration="A stool sits here.",
                still="stills/scene-01.png",
                audio="audio/scene-01.wav",
                still_prompt="a stool", voice_text="A stool sits here.",
                still_seed=12345)])
        target = self.tmp / "film"
        scenes_mod.save_project(project, target)
        loaded = scenes_mod.load_project(target)
        scene = loaded.scenes[0]
        self.assertEqual(scene.still_prompt, "a stool")
        self.assertEqual(scene.voice_text, "A stool sits here.")
        self.assertEqual(scene.still_seed, 12345)

    def test_old_scene_without_provenance_loads_empty(self):
        scene = scenes_mod.Scene.from_dict(
            {"prompt": "a stool", "still": "stills/scene-01.png"})
        self.assertEqual(scene.still_prompt, "")
        self.assertEqual(scene.voice_text, "")
        self.assertEqual(scene.still_seed, 0)

    def test_bad_seed_loads_as_zero(self):
        scene = scenes_mod.Scene.from_dict({"still_seed": "junk"})
        self.assertEqual(scene.still_seed, 0)


class StaleTests(TempEnvTestCase):
    def setUp(self):
        super().setUp()
        self.project_dir = self.tmp / "film"
        paths = scenes_mod.ensure_project_dirs(self.project_dir)
        (paths["stills"] / "scene-01.png").write_bytes(b"fake png")
        (paths["audio"] / "scene-01.wav").write_bytes(b"fake wav")
        (paths["clips"] / "scene-01.mp4").write_bytes(b"fake mp4")
        self.scene = scenes_mod.Scene(
            prompt="a stool", narration="A stool sits here.",
            still="stills/scene-01.png", audio="audio/scene-01.wav",
            clip="clips/scene-01.mp4",
            still_prompt="a stool", voice_text="A stool sits here.")

    def test_matching_provenance_is_fresh(self):
        self.assertFalse(scenes_mod.still_is_stale(
            self.scene, self.project_dir))
        self.assertFalse(scenes_mod.voice_is_stale(
            self.scene, self.project_dir))
        self.assertFalse(scenes_mod.clip_is_stale(
            self.scene, self.project_dir))

    def test_missing_file_is_stale(self):
        scene = scenes_mod.Scene(prompt="a stool",
                                 narration="A stool sits here.")
        self.assertTrue(scenes_mod.still_is_stale(
            scene, self.project_dir))
        self.assertTrue(scenes_mod.voice_is_stale(
            scene, self.project_dir))
        self.assertTrue(scenes_mod.clip_is_stale(
            scene, self.project_dir))

    def test_edited_prompt_makes_still_and_clip_stale(self):
        self.scene.prompt = "a red stool"
        self.assertTrue(scenes_mod.still_is_stale(
            self.scene, self.project_dir))
        self.assertFalse(scenes_mod.voice_is_stale(
            self.scene, self.project_dir))
        self.assertTrue(scenes_mod.clip_is_stale(
            self.scene, self.project_dir))

    def test_edited_line_makes_voice_and_clip_stale(self):
        self.scene.narration = "A different line."
        self.assertFalse(scenes_mod.still_is_stale(
            self.scene, self.project_dir))
        self.assertTrue(scenes_mod.voice_is_stale(
            self.scene, self.project_dir))
        self.assertTrue(scenes_mod.clip_is_stale(
            self.scene, self.project_dir))

    def test_legacy_no_provenance_with_file_is_fresh(self):
        """Old projects carry no provenance: a file that exists is
        kept, not remade."""
        self.scene.still_prompt = ""
        self.scene.voice_text = ""
        self.assertFalse(scenes_mod.still_is_stale(
            self.scene, self.project_dir))
        self.assertFalse(scenes_mod.voice_is_stale(
            self.scene, self.project_dir))
        self.assertFalse(scenes_mod.clip_is_stale(
            self.scene, self.project_dir))


class VoiceAndMotionProvenanceTests(TempEnvTestCase):
    """Items 11, 13: the voice and the camera move are kept per
    project and per scene."""

    def test_project_voice_defaults_to_lessac(self):
        from hicksfield import voice
        project = scenes_mod.Project()
        self.assertEqual(project.voice, "en_US-lessac-medium")
        self.assertEqual(project.voice, voice.VOICE_ID)

    def test_project_voice_round_trip(self):
        project = scenes_mod.Project(voice="en_GB-alan-medium")
        target = self.tmp / "film"
        scenes_mod.save_project(project, target)
        loaded = scenes_mod.load_project(target)
        self.assertEqual(loaded.voice, "en_GB-alan-medium")

    def test_old_project_without_voice_loads_lessac(self):
        import json
        target = self.tmp / "film"
        target.mkdir()
        (target / "project.json").write_text(
            json.dumps({"name": "Old film", "idea": "", "scenes": []}),
            encoding="utf-8")
        loaded = scenes_mod.load_project(target)
        self.assertEqual(loaded.voice, "en_US-lessac-medium")

    def test_voice_and_motion_provenance_round_trip(self):
        project = scenes_mod.Project(
            scenes=[scenes_mod.Scene(
                prompt="a stool", narration="A stool sits here.",
                voice_id="en_US-ryan-high", motion="panleft",
                clip_motion="panleft")])
        target = self.tmp / "film"
        scenes_mod.save_project(project, target)
        scene = scenes_mod.load_project(target).scenes[0]
        self.assertEqual(scene.voice_id, "en_US-ryan-high")
        self.assertEqual(scene.motion, "panleft")
        self.assertEqual(scene.clip_motion, "panleft")

    def test_old_scene_without_voice_or_motion_loads_empty(self):
        scene = scenes_mod.Scene.from_dict(
            {"prompt": "a stool", "audio": "audio/scene-01.wav"})
        self.assertEqual(scene.voice_id, "")
        self.assertEqual(scene.motion, "")
        self.assertEqual(scene.clip_motion, "")


class VoiceMotionStaleTests(TempEnvTestCase):
    """Items 11, 13: a line spoken in another voice, or a clip made
    with another move, is out of date."""

    def setUp(self):
        super().setUp()
        self.project_dir = self.tmp / "film"
        paths = scenes_mod.ensure_project_dirs(self.project_dir)
        (paths["stills"] / "scene-01.png").write_bytes(b"fake png")
        (paths["audio"] / "scene-01.wav").write_bytes(b"fake wav")
        (paths["clips"] / "scene-01.mp4").write_bytes(b"fake mp4")
        self.scene = scenes_mod.Scene(
            prompt="a stool", narration="A stool sits here.",
            still="stills/scene-01.png", audio="audio/scene-01.wav",
            clip="clips/scene-01.mp4",
            still_prompt="a stool", voice_text="A stool sits here.",
            voice_id="en_US-lessac-medium", clip_motion="zoomin")

    def test_matching_voice_and_motion_is_fresh(self):
        self.assertFalse(scenes_mod.voice_is_stale(
            self.scene, self.project_dir, "en_US-lessac-medium"))
        self.assertFalse(scenes_mod.clip_is_stale(
            self.scene, self.project_dir, "en_US-lessac-medium",
            "zoomin"))

    def test_other_voice_makes_line_and_clip_stale(self):
        self.assertTrue(scenes_mod.voice_is_stale(
            self.scene, self.project_dir, "en_US-ryan-high"))
        self.assertTrue(scenes_mod.clip_is_stale(
            self.scene, self.project_dir, "en_US-ryan-high", "zoomin"))

    def test_other_motion_makes_clip_stale_only(self):
        self.assertFalse(scenes_mod.voice_is_stale(
            self.scene, self.project_dir, "en_US-lessac-medium"))
        self.assertTrue(scenes_mod.clip_is_stale(
            self.scene, self.project_dir, "en_US-lessac-medium",
            "panleft"))

    def test_legacy_no_voice_or_motion_is_fresh(self):
        """Old projects carry no voice or move: a file that exists
        is kept, not remade."""
        self.scene.voice_id = ""
        self.scene.clip_motion = ""
        self.assertFalse(scenes_mod.voice_is_stale(
            self.scene, self.project_dir, "en_US-ryan-high"))
        self.assertFalse(scenes_mod.clip_is_stale(
            self.scene, self.project_dir, "en_US-ryan-high", "panleft"))

    def test_no_voice_or_motion_given_keeps_old_behavior(self):
        # Without the current voice and move, only the words decide.
        self.assertFalse(scenes_mod.voice_is_stale(
            self.scene, self.project_dir))
        self.assertFalse(scenes_mod.clip_is_stale(
            self.scene, self.project_dir))


if __name__ == "__main__":
    unittest.main()
