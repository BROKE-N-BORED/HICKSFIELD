"""The film build: stills, voice, motion, assembly, in order.

One job at a time, in a worker thread. Work already done is kept: a scene
that already has its picture, its spoken line, or its clip is skipped and
the skip is listed in the report, never hidden. The project is saved
after each piece lands, so cancelling keeps what is already made.
"""

import time
from pathlib import Path

from hicksfield import scenes as scenes_mod
from hicksfield import video, voice
from hicksfield.errors import HicksFieldError
from hicksfield.steps import run_steps

MIN_SCENE_SECONDS = 3


def _asset_ready(project_dir, relative):
    if not relative:
        return False
    path = Path(project_dir) / relative
    return path.exists() and path.stat().st_size > 0


def build_film(project, project_dir, engine, report=None,
               cancel_event=None, save_cb=None, force_ram=False):
    """Build the film for `project`. Returns the result report dict."""
    project_dir = Path(project_dir)
    if not project.scenes:
        raise HicksFieldError(
            "There are no scenes yet.",
            "Break the idea into scenes, or add scenes by hand first.",
        )
    asset_paths = scenes_mod.ensure_project_dirs(project_dir)
    result = {
        "engine": engine.label,
        "film": "",
        "captions": "",
        "captions_burned": False,
        "stills_made": 0,
        "stills_skipped": 0,
        "voice_made": 0,
        "voice_skipped": 0,
        "clips_made": 0,
        "clips_skipped": 0,
        "skipped": [],
        "durations": [],
        "total_seconds": 0.0,
        "started": time.time(),
        "ended": None,
    }
    state = {"durations": [scenes_mod.DEFAULT_SCENE_SECONDS
                           for _ in project.scenes]}

    def _save():
        if save_cb is not None:
            save_cb(project)

    def _stills(progress, cancel):
        engine.check_ram(force=force_ram)
        total = len(project.scenes)
        for index, scene in enumerate(project.scenes):
            if _asset_ready(project_dir, scene.still):
                result["stills_skipped"] += 1
                result["skipped"].append(
                    f"Scene {index + 1}: kept its picture (already made)."
                )
            else:
                name = scenes_mod.scene_still_name(index)
                out = asset_paths["stills"] / name
                engine.generate(scene.prompt, out,
                                cancel_event=cancel, force=force_ram)
                scene.still = f"{scenes_mod.STILLS_DIR}/{name}"
                result["stills_made"] += 1
                _save()
            progress((index + 1) / total, f"scene {index + 1} of {total}")

    def _voice(progress, cancel):
        total = len(project.scenes)
        for index, scene in enumerate(project.scenes):
            if _asset_ready(project_dir, scene.audio):
                duration = voice.wav_duration(
                    project_dir / scene.audio)
                if duration:
                    state["durations"][index] = max(
                        MIN_SCENE_SECONDS, duration)
                result["voice_skipped"] += 1
                result["skipped"].append(
                    f"Scene {index + 1}: kept its spoken line "
                    "(already made)."
                )
            else:
                name = scenes_mod.scene_audio_name(index)
                out = asset_paths["audio"] / name
                _path, duration = voice.synthesize(
                    scene.narration, out, cancel_event=cancel)
                scene.audio = f"{scenes_mod.AUDIO_DIR}/{name}"
                state["durations"][index] = max(MIN_SCENE_SECONDS, duration)
                result["voice_made"] += 1
                _save()
            progress((index + 1) / total, f"scene {index + 1} of {total}")

    def _motion(progress, cancel):
        total = len(project.scenes)
        for index, scene in enumerate(project.scenes):
            if _asset_ready(project_dir, scene.clip):
                result["clips_skipped"] += 1
                result["skipped"].append(
                    f"Scene {index + 1}: kept its moving clip "
                    "(already made)."
                )
            else:
                name = scenes_mod.scene_clip_name(index)
                out = asset_paths["clips"] / name
                duration = state["durations"][index]
                audio = (project_dir / scene.audio
                         if _asset_ready(project_dir, scene.audio) else None)
                video.make_scene_clip(
                    project_dir / scene.still, out, duration,
                    video.motion_for_scene(index), audio_path=audio,
                    cancel_event=cancel)
                scene.clip = f"{scenes_mod.CLIPS_DIR}/{name}"
                result["clips_made"] += 1
                _save()
            progress((index + 1) / total, f"scene {index + 1} of {total}")

    def _assemble(progress, cancel):
        entries = []
        cursor = 0.0
        for index, scene in enumerate(project.scenes):
            duration = state["durations"][index]
            entries.append((cursor, cursor + duration, scene.narration))
            cursor += duration
        captions = asset_paths["captions"]
        video.write_srt(entries, captions)
        result["captions"] = str(captions)
        font = video.find_caption_font()
        clips = [project_dir / scene.clip for scene in project.scenes]

        def _cb(fraction):
            progress(fraction, "")

        video.assemble(
            clips, asset_paths["film"], srt_path=captions,
            font_path=font, total_seconds=cursor,
            cancel_event=cancel, progress_cb=_cb,
        )
        result["film"] = str(asset_paths["film"])
        result["captions_burned"] = font is not None
        if font is None:
            result["skipped"].append(
                "Captions are in the film.srt file but not burned into "
                "the picture: no caption font was found on this computer."
            )
        result["durations"] = list(state["durations"])
        result["total_seconds"] = cursor
        progress(1.0, "")

    steps = [
        (40, "Making pictures", _stills),
        (10, "Speaking the lines", _voice),
        (30, "Moving the pictures", _motion),
        (20, "Cutting the film together", _assemble),
    ]
    run_steps(steps, report=report, cancel_event=cancel_event)
    result["ended"] = time.time()
    _save()
    return result
