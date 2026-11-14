import hashlib
import os
import unittest
from pathlib import Path

from hicksfield import stills
from hicksfield.stills import (
    FASTSD_CPU_COMMIT,
    FASTSD_MODEL_FILES,
    LCM_MODEL_SHA256,
    LCM_MODEL_SIZE,
    SDCPP_COMMIT,
    SDCPP_RELEASE,
    FastSDCPUEngine,
    RamShortError,
    SDCppEngine,
    check_model_file,
    discover_engines,
    is_image_file,
    preferred_engine,
    sha256_file,
)
from tests.helpers import TempEnvTestCase, write_png


class PinsTests(unittest.TestCase):
    def test_fastsd_commit_is_full_sha(self):
        self.assertEqual(len(FASTSD_CPU_COMMIT), 40)
        int(FASTSD_CPU_COMMIT, 16)

    def test_sdcpp_pins(self):
        self.assertEqual(SDCPP_RELEASE, "master-1086-465adf3")
        self.assertEqual(len(SDCPP_COMMIT), 40)
        int(SDCPP_COMMIT, 16)

    def test_lcm_model_pin(self):
        self.assertEqual(LCM_MODEL_SIZE, 2134675232)
        self.assertEqual(len(LCM_MODEL_SHA256), 64)
        int(LCM_MODEL_SHA256, 16)

    def test_model_files_are_fp16_only(self):
        self.assertEqual(len(FASTSD_MODEL_FILES), 12)
        for name in FASTSD_MODEL_FILES:
            self.assertNotIn("fp32", name, name)
        self.assertIn("model_index.json", FASTSD_MODEL_FILES)
        self.assertIn("unet/diffusion_pytorch_model.fp16.safetensors",
                      FASTSD_MODEL_FILES)


class FileCheckTests(TempEnvTestCase):
    def test_sha256_file(self):
        target = self.tmp / "a.bin"
        target.write_bytes(b"hello")
        self.assertEqual(sha256_file(target),
                         hashlib.sha256(b"hello").hexdigest())

    def test_check_model_file_size_and_sha(self):
        target = self.tmp / "a.bin"
        target.write_bytes(b"hello")
        digest = hashlib.sha256(b"hello").hexdigest()
        ok, _msg = check_model_file(target, 5, digest)
        self.assertTrue(ok)
        ok, _msg = check_model_file(target, 6, digest)
        self.assertFalse(ok)
        ok, msg = check_model_file(target, 5, "0" * 64)
        self.assertFalse(ok)
        self.assertIn("checksum", msg)
        ok, _msg = check_model_file(self.tmp / "missing", 5, digest)
        self.assertFalse(ok)

    def test_is_image_file(self):
        png = write_png(self.tmp / "a.png")
        self.assertTrue(is_image_file(png))
        jpg = self.tmp / "a.jpg"
        jpg.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 20)
        self.assertTrue(is_image_file(jpg))
        text = self.tmp / "a.txt"
        text.write_text("hello", encoding="utf-8")
        self.assertFalse(is_image_file(text))
        self.assertFalse(is_image_file(self.tmp / "missing.png"))


class EngineTests(TempEnvTestCase):
    def test_discover_fast_first(self):
        engines = discover_engines()
        self.assertEqual([e.key for e in engines], ["fastsd", "sdcpp"])
        self.assertEqual(engines[0].label, "Fast stills")

    def test_preferred_none_when_nothing_ready(self):
        os.environ["HICKSFIELD_FASTSD_DIR"] = str(self.tmp / "fast")
        os.environ["HICKSFIELD_SDCPP_DIR"] = str(self.tmp / "steady")
        self.assertIsNone(preferred_engine())

    def test_fast_command_is_offline_one_step(self):
        engine = FastSDCPUEngine(
            base_dir=self.tmp / "fast",
            model_dir=self.tmp / "models" / "sd-turbo")
        cmd = engine.build_command("a stool", seed=42)
        self.assertIn("--use_offline_model", cmd)
        self.assertIn(str(self.tmp / "models" / "sd-turbo"), cmd)
        self.assertIn("--inference_steps", cmd)
        self.assertEqual(cmd[cmd.index("--inference_steps") + 1], "1")
        self.assertEqual(cmd[cmd.index("--guidance_scale") + 1], "1.0")
        self.assertEqual(cmd[cmd.index("--seed") + 1], "42")

    def test_fast_not_ready_without_setup(self):
        engine = FastSDCPUEngine(
            base_dir=self.tmp / "fast",
            model_dir=self.tmp / "models" / "sd-turbo")
        ready, status = engine.is_ready()
        self.assertFalse(ready)
        self.assertIn("Not set up", status)

    def test_cli_save_patch(self):
        from hicksfield.stills import CLI_SAVE_ANCHOR, CLI_SAVE_PATCHED
        engine = FastSDCPUEngine(
            base_dir=self.tmp / "fast",
            model_dir=self.tmp / "models" / "sd-turbo")
        app_py = engine.cli_script
        app_py.parent.mkdir(parents=True, exist_ok=True)
        app_py.write_text("# header\n" + CLI_SAVE_ANCHOR + "# footer\n",
                          encoding="utf-8")
        engine._apply_patch(app_py, CLI_SAVE_ANCHOR, CLI_SAVE_PATCHED)
        text = app_py.read_text(encoding="utf-8")
        self.assertIn(CLI_SAVE_PATCHED, text)
        self.assertIn("save_images(images, config)", text)
        # Idempotent: a second run changes nothing.
        engine._apply_patch(app_py, CLI_SAVE_ANCHOR, CLI_SAVE_PATCHED)
        self.assertEqual(app_py.read_text(encoding="utf-8"), text)

    def test_pipeline_variant_patch(self):
        from hicksfield.stills import (
            PIPELINE_VARIANT_ANCHOR,
            PIPELINE_VARIANT_PATCHED,
        )
        engine = FastSDCPUEngine(
            base_dir=self.tmp / "fast",
            model_dir=self.tmp / "models" / "sd-turbo")
        lcm_py = (engine.checkout / "src" / "backend" / "pipelines"
                  / "lcm.py")
        lcm_py.parent.mkdir(parents=True, exist_ok=True)
        lcm_py.write_text("# header\n" + PIPELINE_VARIANT_ANCHOR
                          + "# footer\n", encoding="utf-8")
        engine._apply_patch(lcm_py, PIPELINE_VARIANT_ANCHOR,
                            PIPELINE_VARIANT_PATCHED)
        text = lcm_py.read_text(encoding="utf-8")
        self.assertIn('variant="fp16"', text)
        self.assertIn("torch_dtype=torch.float16", text)
        engine._apply_patch(lcm_py, PIPELINE_VARIANT_ANCHOR,
                            PIPELINE_VARIANT_PATCHED)
        self.assertEqual(lcm_py.read_text(encoding="utf-8"), text)

    def test_patch_rejects_changed_upstream(self):
        from hicksfield.errors import HicksFieldError
        from hicksfield.stills import CLI_SAVE_ANCHOR, CLI_SAVE_PATCHED
        engine = FastSDCPUEngine(
            base_dir=self.tmp / "fast",
            model_dir=self.tmp / "models" / "sd-turbo")
        app_py = engine.cli_script
        app_py.parent.mkdir(parents=True, exist_ok=True)
        app_py.write_text("# something else entirely\n",
                          encoding="utf-8")
        with self.assertRaises(HicksFieldError):
            engine._apply_patch(app_py, CLI_SAVE_ANCHOR, CLI_SAVE_PATCHED)

    def test_patch_anchors_match_pinned_checkout(self):
        # The anchors (or the patched text, once a setup run has
        # applied them) must be in the real pinned checkout, or setup
        # would fail on the user's machine. Skipped when the probe
        # checkout is not present (it is not part of the tree).
        from hicksfield.stills import (
            CLI_SAVE_ANCHOR,
            CLI_SAVE_PATCHED,
            PIPELINE_VARIANT_ANCHOR,
            PIPELINE_VARIANT_PATCHED,
        )
        probe = (Path(__file__).resolve().parent.parent.parent
                 / "probe" / "fastsdcpu")
        app_py = probe / "src" / "app.py"
        lcm_py = probe / "src" / "backend" / "pipelines" / "lcm.py"
        if not app_py.exists() or not lcm_py.exists():
            self.skipTest("probe checkout not present")
        app_text = app_py.read_text(encoding="utf-8")
        self.assertTrue(CLI_SAVE_ANCHOR in app_text
                        or CLI_SAVE_PATCHED in app_text)
        lcm_text = lcm_py.read_text(encoding="utf-8")
        self.assertTrue(PIPELINE_VARIANT_ANCHOR in lcm_text
                        or PIPELINE_VARIANT_PATCHED in lcm_text)

    def test_fast_model_status_counts(self):
        engine = FastSDCPUEngine(
            base_dir=self.tmp / "fast",
            model_dir=self.tmp / "models" / "sd-turbo")
        have, total, missing = engine.model_status()
        self.assertEqual(have, 0)
        self.assertEqual(total, 12)
        self.assertEqual(len(missing), 12)
        first = engine.model_dir / FASTSD_MODEL_FILES[0]
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_text("{}", encoding="utf-8")
        have, _total, _missing = engine.model_status()
        self.assertEqual(have, 1)

    def test_ram_short_error_has_numbers(self):
        err = RamShortError(3072, 512)
        self.assertIn("3072", str(err))
        self.assertIn("512", str(err))

    def test_sdcpp_find_binary_override(self):
        fake = self.tmp / "sd-cli"
        fake.write_text("x", encoding="utf-8")
        os.environ["HICKSFIELD_SD_CLI"] = str(fake)
        engine = SDCppEngine(base_dir=self.tmp / "steady")
        self.assertEqual(engine.find_binary(), fake)

    def test_sdcpp_command_uses_mmap_and_lcm(self):
        engine = SDCppEngine(base_dir=self.tmp / "steady")
        cmd = engine.build_command(
            self.tmp / "sd-cli", self.tmp / "model.gguf",
            "a stool", 42, 512, self.tmp / "out.png")
        self.assertIn("--mmap", cmd)
        self.assertIn("--sampling-method", cmd)
        self.assertEqual(cmd[cmd.index("--sampling-method") + 1], "lcm")
        self.assertEqual(cmd[cmd.index("--steps") + 1], "4")
        self.assertEqual(cmd[cmd.index("--cfg-scale") + 1], "1.5")

    def test_sdcpp_find_model_rejects_wrong_size(self):
        fake = self.tmp / "model.gguf"
        fake.write_bytes(b"tiny")
        os.environ["HICKSFIELD_SD_MODEL"] = str(fake)
        engine = SDCppEngine(base_dir=self.tmp / "steady")
        # Point the shared homes at an empty temp dir so a model another
        # project left on this machine cannot leak into the test.
        engine.model_homes = lambda: [self.tmp / "models"]
        found, verified = engine.find_model()
        self.assertIsNone(found)
        self.assertFalse(verified)

    def test_sdcpp_find_model_ignores_wrong_size_in_home(self):
        # A same-named file of the wrong size in a model home is not
        # a model: find_model skips it and reports nothing found.
        target = self.tmp / "models" / "LCM_Dreamshaper_v7-f16.gguf"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 100)
        engine = SDCppEngine(base_dir=self.tmp / "steady")
        engine.model_homes = lambda: [self.tmp / "models"]
        found, verified = engine.find_model()
        self.assertIsNone(found)
        self.assertFalse(verified)


if __name__ == "__main__":
    unittest.main()
