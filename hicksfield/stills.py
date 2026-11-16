"""Stills engines: make one picture per scene, on CPU.

Two engines, one interface. The fast engine drives FastSD CPU (1-step
distilled image models, LCM mode). The steady engine drives
stable-diffusion.cpp (a small C++ image tool) with an LCM model. The app
uses whichever engine is ready, fast first.

Both engines are set up from inside the app, with real progress and
cancel, and both check RAM before generating. Nothing here downloads
anything without being asked.
"""

import hashlib
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

from hicksfield import machine, paths
from hicksfield.downloader import download
from hicksfield.errors import (
    CancelledError,
    DownloadError,
    EngineNotReadyError,
    HicksFieldError,
)
from hicksfield.steps import run_steps

logger = logging.getLogger("hicksfield.stills")

STILL_SIZE = 512
PER_IMAGE_TIMEOUT_S = 1800

# --- FastSD CPU (fast engine) -------------------------------------------
# Pinned to the commit probed 2026-11-13. The checkout is fetched as a
# tarball so setup does not need git.
FASTSD_CPU_COMMIT = "10ed947f16cd35479a1d202be4e1309e243fd767"
FASTSD_CPU_TARBALL = (
    "https://codeload.github.com/rupeshs/fastsdcpu/tar.gz/"
    + FASTSD_CPU_COMMIT
)
FASTSD_MODEL_ID = "stabilityai/sd-turbo"
# The fp16 files the diffusers pipeline loads on CPU. The fp32 twins and
# the single-file safetensors are skipped on purpose: they would add
# about 5 GB for files the pipeline never reads.
FASTSD_MODEL_FILES = [
    "model_index.json",
    "scheduler/scheduler_config.json",
    "text_encoder/config.json",
    "text_encoder/model.fp16.safetensors",
    "tokenizer/merges.txt",
    "tokenizer/special_tokens_map.json",
    "tokenizer/tokenizer_config.json",
    "tokenizer/vocab.json",
    "unet/config.json",
    "unet/diffusion_pytorch_model.fp16.safetensors",
    "vae/config.json",
    "vae/diffusion_pytorch_model.fp16.safetensors",
]
FASTSD_STEPS = 1
FASTSD_GUIDANCE = 1.0
FASTSD_REQUIRED_RAM_MB = 3072
# The stock non-interactive CLI generates the picture and throws it
# away: its final loop calls generate_text_to_image and never calls
# save_images (every other interface in the same app does). Setup
# patches the vendored copy so the picture lands in results/.
CLI_SAVE_ANCHOR = (
    "        else:\n"
    "            for i in range(0, args.batch_count):\n"
    "                context.generate_text_to_image(\n"
    "                    settings=config,\n"
    "                    device=DEVICE,\n"
    "                )\n"
)
CLI_SAVE_PATCHED = (
    "        else:\n"
    "            for i in range(0, args.batch_count):\n"
    "                images = context.generate_text_to_image(\n"
    "                    settings=config,\n"
    "                    device=DEVICE,\n"
    "                )\n"
    "                if images:\n"
    "                    context.save_images(images, config)\n"
)
# The stock pipeline loader passes no dtype and no variant, so it
# looks for the full-precision weight files. HicksField downloads
# only the fp16 files (the full set would add about 4 GB and load
# in full precision first, blowing the RAM budget). Setup patches
# the vendored loader to ask for the fp16 files directly.
PIPELINE_VARIANT_ANCHOR = (
    "        # pipeline = DiffusionPipeline.from_pretrained(\n"
    "        pipeline = AutoPipelineForText2Image.from_pretrained(\n"
    "            model_id,\n"
    "            local_files_only=use_local_model,\n"
    "            **pipeline_args,\n"
    "        )\n"
)
PIPELINE_VARIANT_PATCHED = (
    "        # pipeline = DiffusionPipeline.from_pretrained(\n"
    "        pipeline = AutoPipelineForText2Image.from_pretrained(\n"
    "            model_id,\n"
    "            local_files_only=use_local_model,\n"
    "            variant=\"fp16\",\n"
    "            torch_dtype=torch.float16,\n"
    "            **pipeline_args,\n"
    "        )\n"
)

# --- stable-diffusion.cpp (steady engine) --------------------------------
SDCPP_REPO = "https://github.com/leejet/stable-diffusion.cpp"
SDCPP_RELEASE = "master-1086-465adf3"
SDCPP_COMMIT = "465adf389f252e401257f44efb0ea49da8e4e80b"
LCM_MODEL_FILENAME = "LCM_Dreamshaper_v7-f16.gguf"
LCM_MODEL_URL = (
    "https://huggingface.co/Steward/lcm-dreamshaper-v7-gguf/resolve/main/"
    + LCM_MODEL_FILENAME
)
LCM_MODEL_SIZE = 2134675232
LCM_MODEL_SHA256 = (
    "0642060766117fdeb8cae0ccdc73ee974ac5779be1112d0a82f2b965bbd00e15"
)
LCM_STEPS = 4
LCM_CFG = 1.5
LCM_NEGATIVE = "blurry, deformed, distorted, low quality, watermark"
SDCPP_REQUIRED_RAM_MB = 3072

_verified_cache = {}


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_model_file(path, expected_size, expected_sha256=None):
    """Size check (cheap) plus optional SHA-256 check (cached per run).

    Returns (ok, message). The hash is only computed once per file per
    process, keyed on path, size, and mtime.
    """
    path = Path(path)
    if not path.exists():
        return False, "the file is not there"
    stat = path.stat()
    if stat.st_size != expected_size:
        return False, (
            f"the file is {stat.st_size} bytes but should be "
            f"{expected_size} bytes"
        )
    if expected_sha256 is None:
        return True, "size matches"
    key = (str(path), stat.st_size, stat.st_mtime_ns, expected_sha256)
    cached = _verified_cache.get(key)
    if cached is not None:
        return cached, "checked earlier"
    actual = sha256_file(path)
    ok = actual == expected_sha256
    _verified_cache[key] = ok
    if ok:
        return True, "size and checksum match"
    return False, "the checksum does not match (the file may be damaged)"


def is_image_file(path):
    """True when the file starts with a PNG or JPEG magic number."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(8)
    except OSError:
        return False
    return head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff")


def head_content_length(url, timeout=30):
    """Content-Length from a HEAD request, or None when unknown."""
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            length = response.headers.get("Content-Length")
            return int(length) if length is not None else None
    except (OSError, ValueError):
        return None


def _tail_lines(text, count=12):
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-count:]


def run_logged(cmd, cwd=None, env=None, timeout_s=PER_IMAGE_TIMEOUT_S,
               cancel_event=None):
    """Run a long command with output to a temp file, not a pipe.

    A pipe nobody reads fills up and deadlocks a long run. The temp file
    keeps the last lines for the failure message. Returns (returncode,
    tail_text). Raises CancelledError when the cancel event is set.
    """
    log = tempfile.TemporaryFile(mode="w+")
    try:
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, env=env, stdout=log,
                stderr=subprocess.STDOUT, text=True,
            )
        except OSError as exc:
            return None, f"could not start the command: {exc}"
        deadline = time.monotonic() + timeout_s
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                proc.kill()
                proc.wait()
                raise CancelledError()
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait()
                return None, "the command ran past its time limit"
            time.sleep(0.2)
        log.seek(0)
        return proc.returncode, "\n".join(_tail_lines(log.read()))
    finally:
        log.close()


def _kill_after(proc, seconds=5):
    try:
        proc.terminate()
        proc.wait(timeout=seconds)
    except (subprocess.TimeoutExpired, OSError):
        try:
            proc.kill()
            proc.wait()
        except OSError:
            pass


class RamShortError(HicksFieldError):
    """Free RAM is below what the engine wants. Warn, never refuse: the
    caller asks the user, and a yes retries with force=True."""

    def __init__(self, required_mb, available_mb):
        self.required_mb = required_mb
        self.available_mb = available_mb
        super().__init__(
            f"Only {available_mb} MB of RAM is free, and making a picture "
            f"needs about {required_mb} MB.",
            "Close other programs to free RAM, or try anyway.",
        )


class StillEngine:
    """One stills engine: setup, readiness, and one picture at a time."""

    key = "base"
    label = "Stills engine"
    description = ""

    @property
    def required_ram_mb(self):
        return 0

    def is_ready(self):
        """Return (ready, plain-words status)."""
        raise NotImplementedError

    def setup(self, report=None, cancel_event=None):
        """Set the engine up, reporting (label, fraction, detail)."""
        raise NotImplementedError

    def generate(self, prompt, out_path, seed=-1, cancel_event=None,
                 force=False):
        """Make one picture. Returns the output Path."""
        raise NotImplementedError

    def check_ram(self, force=False):
        ok, available, _message = machine.ram_check(self.required_ram_mb)
        if not ok and not force:
            raise RamShortError(self.required_ram_mb, available)
        return available


class FastSDCPUEngine(StillEngine):
    """Fast engine: FastSD CPU, 1-step distilled models, LCM mode."""

    key = "fastsd"
    label = "Fast stills"
    description = (
        "1-step picture models, made for CPU. A picture takes seconds, "
        "not minutes."
    )

    def __init__(self, base_dir=None, model_dir=None):
        if base_dir is None:
            override = os.environ.get("HICKSFIELD_FASTSD_DIR")
            base_dir = Path(override) if override else (
                paths.engines_dir() / "fastsd-cpu"
            )
        self.base_dir = Path(base_dir)
        self.checkout = self.base_dir / "fastsdcpu"
        self.env_dir = self.base_dir / "env"
        if model_dir is None:
            override = os.environ.get("HICKSFIELD_FASTSD_MODEL")
            model_dir = Path(override) if override else (
                paths.models_dir() / "fastsd" / "sd-turbo"
            )
        self.model_dir = Path(model_dir)

    @property
    def required_ram_mb(self):
        return FASTSD_REQUIRED_RAM_MB

    @property
    def cli_python(self):
        return self.env_dir / "bin" / "python"

    @property
    def cli_script(self):
        return self.checkout / "src" / "app.py"

    @property
    def results_dir(self):
        return self.checkout / "results"

    def model_files(self):
        base = (
            "https://huggingface.co/" + FASTSD_MODEL_ID
            + "/resolve/main/"
        )
        return [(name, base + name) for name in FASTSD_MODEL_FILES]

    def model_status(self):
        missing = [name for name, _url in self.model_files()
                   if not (self.model_dir / name).exists()
                   or (self.model_dir / name).stat().st_size == 0]
        total = len(FASTSD_MODEL_FILES)
        return total - len(missing), total, missing

    def is_ready(self):
        if not self.cli_script.exists():
            return False, "Not set up yet."
        if not self.cli_python.exists():
            return False, "Set up halfway: the engine home is missing."
        have, total, _missing = self.model_status()
        if have < total:
            return False, (
                f"Set up halfway: the picture model is {have} of {total} "
                "files downloaded."
            )
        return True, "Ready."

    def setup(self, report=None, cancel_event=None):
        steps = [
            (2, "Getting the stills engine", self._setup_checkout),
            (3, "Making a home for the engine", self._setup_venv),
            (20, "Installing the engine pieces", self._setup_packages),
            (75, "Downloading the picture model", self._setup_model),
        ]
        run_steps(steps, report=report, cancel_event=cancel_event)

    def _setup_checkout(self, progress, _cancel):
        if not self.cli_script.exists():
            self.base_dir.mkdir(parents=True, exist_ok=True)
            tarball = self.base_dir / "fastsdcpu.tar.gz"
            download(FASTSD_CPU_TARBALL, tarball, progress_cb=None)
            progress(0.5, "unpacking")
            with tarfile.open(tarball, "r:gz") as archive:
                archive.extractall(self.base_dir)
            extracted = self.base_dir / f"fastsdcpu-{FASTSD_CPU_COMMIT}"
            if extracted.exists():
                if self.checkout.exists():
                    shutil.rmtree(self.checkout)
                extracted.rename(self.checkout)
            tarball.unlink(missing_ok=True)
            if not self.cli_script.exists():
                raise DownloadError(
                    "The stills engine download did not contain the files "
                    "it should have.",
                    "Try setting up the engine again.",
                )
        self._patch_checkout()
        progress(1.0, "")

    def _patch_checkout(self):
        """Apply the two vendored fixes (see the constants above).

        Both are idempotent; if the upstream text ever changes, setup
        fails loudly instead of silently generating nothing.
        """
        self._apply_patch(
            self.cli_script, CLI_SAVE_ANCHOR, CLI_SAVE_PATCHED)
        self._apply_patch(
            self.checkout / "src" / "backend" / "pipelines" / "lcm.py",
            PIPELINE_VARIANT_ANCHOR, PIPELINE_VARIANT_PATCHED)

    @staticmethod
    def _apply_patch(path, anchor, patched):
        text = Path(path).read_text(encoding="utf-8")
        if patched in text:
            return
        if anchor not in text:
            raise HicksFieldError(
                "The stills engine changed in a way HicksField did not "
                "expect.",
                "Try setting up the engine again.",
            )
        Path(path).write_text(
            text.replace(anchor, patched, 1), encoding="utf-8")

    def _setup_venv(self, progress, _cancel):
        if self.cli_python.exists():
            progress(1.0, "already there")
            return
        self.base_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [sys.executable, "-m", "venv", str(self.env_dir)],
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0 or not self.cli_python.exists():
            raise HicksFieldError(
                "Could not make a home for the stills engine.",
                "Run the HicksField installer again. It installs the "
                "Python venv package for you.",
            )
        progress(1.0, "")

    def _setup_packages(self, progress, cancel_event):
        marker = self.env_dir / ".hicksfield-packages-ok"
        if marker.exists() and self.cli_python.exists():
            progress(1.0, "already there")
            return
        progress(None, "getting the number-crunching pieces")
        pip = [str(self.cli_python), "-m", "pip", "install", "--quiet"]
        code, tail = run_logged(
            pip + ["torch==2.8.0", "torchvision==0.23.0",
                   "--index-url",
                   "https://download.pytorch.org/whl/cpu"],
            timeout_s=1800, cancel_event=cancel_event,
        )
        if code != 0:
            raise HicksFieldError(
                "Could not install the number-crunching pieces.",
                "Check the internet connection and try again. "
                f"Last lines: {tail}",
            )
        requirements = self.checkout / "requirements.txt"
        wanted = []
        for line in requirements.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # PyQt5 is the other app's own window toolkit; HicksField has
            # its own window and never starts it. Pillow is pinned to a
            # version with no Python 3.12 wheel; the newer Pillow that
            # comes with the number-crunching pieces works fine.
            if line.startswith("PyQt5") or line.startswith("Pillow"):
                continue
            wanted.append(line)
        progress(None, "getting the rest of the pieces")
        code, tail = run_logged(
            pip + wanted, timeout_s=1800, cancel_event=cancel_event,
        )
        if code != 0:
            raise HicksFieldError(
                "Could not install the engine pieces.",
                "Check the internet connection and try again. "
                f"Last lines: {tail}",
            )
        marker.write_text("ok", encoding="utf-8")
        progress(1.0, "")

    def _setup_model(self, progress, cancel_event):
        files = self.model_files()
        sizes = {}
        total = 0
        for name, url in files:
            size = head_content_length(url)
            if size:
                sizes[name] = size
                total += size
        downloaded = 0
        for name, url in files:
            dest = self.model_dir / name
            if dest.exists() and dest.stat().st_size > 0:
                downloaded += sizes.get(name, 0)
                continue
            expected = sizes.get(name)

            def _cb(done, _total, _name=name, _expected=expected,
                    _base=downloaded):
                if total:
                    progress((_base + done) / total, _name)

            download(url, dest, expected_size=expected,
                     progress_cb=_cb if total else None,
                     cancel_event=cancel_event)
            downloaded += sizes.get(name, dest.stat().st_size)
        have, total_count, missing = self.model_status()
        if have < total_count:
            raise DownloadError(
                "The picture model is still missing files: "
                + ", ".join(missing[:3]) + ".",
                "Try the download again. It picks up where it left off.",
            )
        progress(1.0, "")

    def build_command(self, prompt, seed=-1):
        cmd = [
            str(self.cli_python), str(self.cli_script),
            "--prompt", prompt,
            "--lcm_model_id", str(self.model_dir),
            "--use_offline_model",
            "--inference_steps", str(FASTSD_STEPS),
            "--guidance_scale", str(FASTSD_GUIDANCE),
            "--image_width", str(STILL_SIZE),
            "--image_height", str(STILL_SIZE),
            "--number_of_images", "1",
        ]
        if seed >= 0:
            cmd += ["--seed", str(seed)]
        return cmd

    def _snapshot_results(self):
        if not self.results_dir.exists():
            return {}
        return {p.name: p.stat().st_mtime_ns
                for p in self.results_dir.iterdir() if p.is_file()}

    def generate(self, prompt, out_path, seed=-1, cancel_event=None,
                 force=False):
        self.check_ram(force=force)
        ready, status = self.is_ready()
        if not ready:
            raise EngineNotReadyError(
                f"The fast stills engine is not ready: {status}",
                "Set it up from the Stills window first.",
            )
        logger.info("Making a picture with %s", self.label)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        before = self._snapshot_results()
        started = time.time()
        env = dict(os.environ)
        threads = str(machine.worker_threads())
        env.update({
            "OMP_NUM_THREADS": threads,
            "MKL_NUM_THREADS": threads,
            "TOKENIZERS_PARALLELISM": "false",
            "HF_HUB_OFFLINE": "1",
            "PYTHONUNBUFFERED": "1",
        })
        # Generation runs offline, so proxy settings are not needed here,
        # and a malformed proxy setting crashes the engine's own web
        # toolkit at import time (proven in the sandbox: httpx rejects
        # bracketed IPv6 entries in NO_PROXY). Strip them for this
        # subprocess only; downloads elsewhere keep the real settings.
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY",
                    "HTTPS_PROXY", "ALL_PROXY", "all_proxy",
                    "NO_PROXY", "no_proxy"):
            env.pop(key, None)
        code, tail = run_logged(
            self.build_command(prompt, seed),
            cwd=str(self.checkout), env=env,
            timeout_s=PER_IMAGE_TIMEOUT_S, cancel_event=cancel_event,
        )
        if code != 0:
            raise HicksFieldError(
                "The stills engine stopped with an error.",
                f"Last lines: {tail}" if tail else "Try again.",
            )
        after = self._snapshot_results()
        new_names = [name for name, mtime in after.items()
                     if name not in before or mtime > before[name]]
        candidates = []
        for name in new_names:
            candidate = self.results_dir / name
            if candidate.suffix.lower() in (".png", ".jpg", ".jpeg"):
                candidates.append(candidate)
        if not candidates:
            # Fall back to the newest image in the folder, made during
            # this run.
            for path in sorted(self.results_dir.iterdir(),
                               key=lambda p: p.stat().st_mtime_ns,
                               reverse=True):
                if (path.suffix.lower() in (".png", ".jpg", ".jpeg")
                        and path.stat().st_mtime >= started - 5):
                    candidates.append(path)
                    break
        if not candidates:
            raise HicksFieldError(
                "The stills engine finished but no picture appeared.",
                f"Last lines: {tail}" if tail else "Try again.",
            )
        newest = max(candidates, key=lambda p: p.stat().st_mtime_ns)
        if not is_image_file(newest):
            raise HicksFieldError(
                "The stills engine finished but the picture file is not "
                "a picture.",
                "Try again.",
            )
        shutil.copyfile(newest, out_path)
        logger.info("Picture made with %s", self.label)
        return out_path


class SDCppEngine(StillEngine):
    """Steady engine: stable-diffusion.cpp with an LCM model."""

    key = "sdcpp"
    label = "Steady stills"
    description = (
        "A small C++ image tool. Slower per picture, but simple and "
        "steady."
    )

    def __init__(self, base_dir=None, binary=None, model=None):
        if base_dir is None:
            override = os.environ.get("HICKSFIELD_SDCPP_DIR")
            base_dir = Path(override) if override else (
                paths.engines_dir() / "sdcpp"
            )
        self.base_dir = Path(base_dir)
        self.checkout = self.base_dir / "sd.cpp"
        self._binary_override = Path(binary) if binary else None
        self._model_override = Path(model) if model else None

    @property
    def required_ram_mb(self):
        return SDCPP_REQUIRED_RAM_MB

    @property
    def binary_path(self):
        return self.checkout / "build" / "bin" / "sd-cli"

    def find_binary(self):
        override = os.environ.get("HICKSFIELD_SD_CLI")
        if override and Path(override).exists():
            return Path(override)
        if self._binary_override is not None and self._binary_override.exists():
            return self._binary_override
        if self.binary_path.exists():
            return self.binary_path
        found = shutil.which("sd-cli") or shutil.which("sd")
        return Path(found) if found else None

    def model_homes(self):
        homes = [paths.models_dir()]
        home = Path.home()
        homes.append(home / "design-shell-models")
        homes.append(home / "pixel-forge-models")
        return homes

    def find_model(self):
        """Return (path, verified) for a size-checked model, or (None, False)."""
        override = os.environ.get("HICKSFIELD_SD_MODEL")
        candidates = []
        if override:
            candidates.append(Path(override))
        if self._model_override is not None:
            candidates.append(self._model_override)
        candidates += [h / LCM_MODEL_FILENAME for h in self.model_homes()]
        for candidate in candidates:
            if not candidate.exists():
                continue
            ok, _msg = check_model_file(
                candidate, LCM_MODEL_SIZE, LCM_MODEL_SHA256
            )
            if ok:
                return candidate, True
            # A size-matching file with a bad checksum is damaged: keep
            # looking, but remember it so the status can say so.
        for candidate in candidates:
            if (candidate.exists()
                    and candidate.stat().st_size == LCM_MODEL_SIZE):
                return candidate, False
        return None, False

    def is_ready(self):
        binary = self.find_binary()
        if binary is None:
            return False, "Not set up yet: the image tool is missing."
        model, verified = self.find_model()
        if model is None:
            return False, "Set up halfway: the picture model is missing."
        if not verified:
            return False, (
                "The picture model file is damaged (its checksum does "
                "not match)."
            )
        return True, "Ready."

    def setup(self, report=None, cancel_event=None):
        steps = [
            (5, "Getting the image tool", self._setup_checkout),
            (40, "Building the image tool", self._setup_build),
            (55, "Downloading the picture model", self._setup_model),
        ]
        run_steps(steps, report=report, cancel_event=cancel_event)

    def _setup_checkout(self, progress, _cancel):
        if self.checkout.exists() and self.binary_path.exists():
            progress(1.0, "already there")
            return
        if shutil.which("git") is None:
            raise HicksFieldError(
                "git is not installed, and the image tool comes from git.",
                "Install it with: sudo apt install git",
            )
        self.base_dir.mkdir(parents=True, exist_ok=True)
        if not self.checkout.exists():
            code, tail = run_logged(
                ["git", "clone", "--branch", SDCPP_RELEASE, "--depth", "1",
                 "--recurse-submodules", SDCPP_REPO, str(self.checkout)],
                timeout_s=900,
            )
            if code != 0 or not self.checkout.exists():
                raise DownloadError(
                    "Could not get the image tool.",
                    "Check the internet connection and try again. "
                    f"Last lines: {tail}",
                )
        progress(1.0, "")

    def _setup_build(self, progress, cancel_event):
        if self.binary_path.exists():
            progress(1.0, "already built")
            return
        if shutil.which("cmake") is None:
            raise HicksFieldError(
                "cmake is not installed, and the image tool needs it "
                "to build.",
                "Install it with: sudo apt install cmake g++",
            )
        jobs = str(machine.worker_threads())
        progress(None, "setting up the build")
        code, tail = run_logged(
            ["cmake", "-B", "build", "-DCMAKE_BUILD_TYPE=Release"],
            cwd=str(self.checkout), timeout_s=600,
            cancel_event=cancel_event,
        )
        if code != 0:
            raise HicksFieldError(
                "The image tool build could not be set up.",
                f"Last lines: {tail}" if tail else "Try again.",
            )
        progress(None, "building (this takes a while, one time only)")
        code, tail = run_logged(
            ["cmake", "--build", "build", "-j", jobs],
            cwd=str(self.checkout), timeout_s=3600,
            cancel_event=cancel_event,
        )
        if code != 0 or not self.binary_path.exists():
            raise HicksFieldError(
                "The image tool did not build.",
                f"Last lines: {tail}" if tail else "Try again.",
            )
        progress(1.0, "")

    def _setup_model(self, progress, cancel_event):
        model, verified = self.find_model()
        if model is not None and verified:
            progress(1.0, "already there")
            return
        dest = paths.models_dir() / LCM_MODEL_FILENAME
        if model is not None and not verified and model != dest:
            # A damaged copy elsewhere: download a fresh one here.
            pass

        def _cb(done, total):
            if total:
                progress(done / total, "")

        download(LCM_MODEL_URL, dest, expected_size=LCM_MODEL_SIZE,
                 progress_cb=_cb, cancel_event=cancel_event)
        ok, msg = check_model_file(dest, LCM_MODEL_SIZE, LCM_MODEL_SHA256)
        if not ok:
            dest.unlink(missing_ok=True)
            raise DownloadError(
                f"The picture model download failed its check: {msg}.",
                "Try the download again.",
            )
        progress(1.0, "")

    def build_command(self, engine, model, prompt, seed, size, out):
        # --mmap keeps the model weights file-backed instead of copying
        # them into anonymous RAM: the weights are 2.1 GB, and on an
        # 8 GB PC the copy is what pushes a generation into the
        # out-of-memory killer. mmap is off by default in this
        # sd.cpp release (model_manager.h: enable_mmap_ = false).
        return [str(engine), "-m", str(model),
                "-p", prompt, "-n", LCM_NEGATIVE,
                "--steps", str(LCM_STEPS), "--cfg-scale", str(LCM_CFG),
                "--sampling-method", "lcm",
                "--mmap",
                "-W", str(size), "-H", str(size),
                "-t", str(machine.worker_threads()),
                "-s", str(seed), "-o", str(out)]

    def generate(self, prompt, out_path, seed=-1, cancel_event=None,
                 force=False):
        self.check_ram(force=force)
        binary = self.find_binary()
        model, verified = self.find_model()
        if binary is None or model is None or not verified:
            _ready, status = self.is_ready()
            raise EngineNotReadyError(
                f"The steady stills engine is not ready: {status}",
                "Set it up from the Stills window first.",
            )
        logger.info("Making a picture with %s", self.label)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if seed < 0:
            seed = random.randint(1, 2**31 - 1)
        code, tail = run_logged(
            self.build_command(binary, model, prompt, seed,
                               STILL_SIZE, out_path),
            timeout_s=PER_IMAGE_TIMEOUT_S, cancel_event=cancel_event,
        )
        if code != 0:
            raise HicksFieldError(
                "The stills engine stopped with an error.",
                f"Last lines: {tail}" if tail else "Try again.",
            )
        if not out_path.exists() or not is_image_file(out_path):
            raise HicksFieldError(
                "The stills engine finished but no picture appeared.",
                f"Last lines: {tail}" if tail else "Try again.",
            )
        logger.info("Picture made with %s", self.label)
        return out_path


def discover_engines():
    """Both engines, fast first."""
    return [FastSDCPUEngine(), SDCppEngine()]


def recommended_engine():
    """The engine to start with, plus the plain-words reason.

    Fast first: it makes a picture in seconds. When only the steady
    engine is ready, it is the one to start with, because it is the
    one that works. Both engines need the same RAM, so readiness, not
    memory, decides.
    """
    engines = discover_engines()
    fast_ready, _s = engines[0].is_ready()
    steady_ready, _s = engines[1].is_ready()
    if fast_ready:
        return engines[0], "Fast is the one to start with on this PC."
    if steady_ready:
        return engines[1], "Steady is the one ready on this PC."
    return engines[0], "Fast is the one to start with on this PC."


def preferred_engine():
    """The first ready engine, fast first, or None."""
    for engine in discover_engines():
        ready, _status = engine.is_ready()
        if ready:
            return engine
    return None
