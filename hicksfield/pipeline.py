"""The film build: stills, voice, motion, assembly, in order.

One job at a time, in a worker thread. Work already done is kept: a
scene whose picture, spoken line, or clip is still fresh is skipped
and the skip is listed in the report, never hidden. Fresh means the
file is there and it was made from the scene's current words, in the
project's current voice, and (for a clip) with the scene's current
camera move: an asset made from older words is remade, and a clip
whose picture or line was remade is remade too. The project is saved
after each piece lands, so cancelling keeps what is already made.
"""

import logging
import time
from pathlib import Path

from hicksfield import scenes as scenes_mod
from hicksfield import video, voice
from hicksfield.errors import HicksFieldError
from hicksfield.steps import run_steps

logger = logging.getLogger("hicksfield.pipeline")

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
    logger.info("Building film: %d scenes, engine %s",
                len(project.scenes), engine.label)
    # Fix each scene's asset identity before anything is made, so a
    # scene that was reordered keeps its own filenames.
    scenes_mod.ensure_asset_ids(project.scenes)
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
    # Scene indexes whose picture or line was (re)made in this run:
    # their clips are rebuilt too, even when the clip file exists.
    remade = set()

    def _save():
        if save_cb is not None:
            save_cb(project)

    def _stills(progress, cancel):
        logger.info("Step started: Making pictures")
        engine.check_ram(force=force_ram)
        total = len(project.scenes)
        for index, scene in enumerate(project.scenes):
            if not scenes_mod.still_is_stale(scene, project_dir):
                result["stills_skipped"] += 1
                result["skipped"].append(
                    f"Scene {index + 1}: kept its picture (already made)."
                )
                logger.info("Scene %d: kept its picture (already made)",
                            index + 1)
            else:
                if not scene.prompt or not scene.prompt.strip():
                    # A scene with no picture words cannot make a
                    # picture (HowTo): the empty prompt is never
                    # sent to the engine. Work made before this
                    # scene is already saved and kept.
                    raise HicksFieldError(
                        f"Scene {index + 1} has no picture words yet.",
                        "Type the picture words for that scene in "
                        "the Scenes window, then build again.")
                was_there = _asset_ready(project_dir, scene.still)
                name = scenes_mod.scene_still_name_for(scene, index)
                out = asset_paths["stills"] / name
                # A remake keeps the recorded seed: only the prompt
                # changed, so the picture keeps its composition. A
                # first make gets a fresh explicit seed, recorded
                # below, so the picture can be made again.
                seed = (scene.still_seed if scene.still_seed
                        else scenes_mod.new_seed())
                scenes_mod.archive_previous_asset(out)
                engine.generate(scene.prompt, out, seed=seed,
                                cancel_event=cancel, force=force_ram)
                scene.still = f"{scenes_mod.STILLS_DIR}/{name}"
                scene.still_prompt = scene.prompt
                scene.still_seed = seed
                remade.add(index)
                result["stills_made"] += 1
                if was_there:
                    logger.info(
                        "Scene %d: remade its picture "
                        "(the prompt changed)", index + 1)
                _save()
            progress((index + 1) / total, f"scene {index + 1} of {total}")
        logger.info("Step finished: Making pictures (%d made, %d kept)",
                    result["stills_made"], result["stills_skipped"])

    def _voice(progress, cancel):
        logger.info("Step started: Speaking the lines")
        total = len(project.scenes)
        for index, scene in enumerate(project.scenes):
            if not scenes_mod.voice_is_stale(scene, project_dir,
                                             project.voice):
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
                logger.info("Scene %d: kept its spoken line (already made)",
                            index + 1)
            else:
                was_there = _asset_ready(project_dir, scene.audio)
                old_voice = scene.voice_id
                name = scenes_mod.scene_audio_name_for(scene, index)
                out = asset_paths["audio"] / name
                _path, duration = voice.synthesize(
                    scene.narration, out, cancel_event=cancel,
                    voice_id=project.voice)
                scene.audio = f"{scenes_mod.AUDIO_DIR}/{name}"
                scene.voice_text = scene.narration
                scene.voice_id = project.voice
                remade.add(index)
                state["durations"][index] = max(MIN_SCENE_SECONDS, duration)
                result["voice_made"] += 1
                if was_there:
                    if old_voice and old_voice != project.voice:
                        reason = "the voice changed"
                    else:
                        reason = "the line changed"
                    logger.info(
                        "Scene %d: remade its spoken line "
                        "(%s)", index + 1, reason)
                _save()
            progress((index + 1) / total, f"scene {index + 1} of {total}")
        logger.info("Step finished: Speaking the lines (%d made, %d kept)",
                    result["voice_made"], result["voice_skipped"])

    def _motion(progress, cancel):
        logger.info("Step started: Moving the pictures")
        total = len(project.scenes)
        for index, scene in enumerate(project.scenes):
            motion = video.motion_for_scene(index, scene.motion)
            if (index not in remade
                    and not scenes_mod.clip_is_stale(
                        scene, project_dir, project.voice, motion)):
                result["clips_skipped"] += 1
                result["skipped"].append(
                    f"Scene {index + 1}: kept its moving clip "
                    "(already made)."
                )
                logger.info("Scene %d: kept its moving clip (already made)",
                            index + 1)
            else:
                if _asset_ready(project_dir, scene.clip):
                    if (scene.clip_motion
                            and scene.clip_motion != motion):
                        reason = "the camera move changed"
                    else:
                        reason = "its picture or line changed"
                    logger.info(
                        "Scene %d: remade its moving clip "
                        "(%s)", index + 1, reason)
                name = scenes_mod.scene_clip_name_for(scene, index)
                out = asset_paths["clips"] / name
                duration = state["durations"][index]
                audio = (project_dir / scene.audio
                         if _asset_ready(project_dir, scene.audio) else None)
                video.make_scene_clip(
                    project_dir / scene.still, out, duration,
                    motion, audio_path=audio,
                    cancel_event=cancel)
                scene.clip = f"{scenes_mod.CLIPS_DIR}/{name}"
                scene.clip_motion = motion
                result["clips_made"] += 1
                _save()
            progress((index + 1) / total, f"scene {index + 1} of {total}")
        logger.info("Step finished: Moving the pictures (%d made, %d kept)",
                    result["clips_made"], result["clips_skipped"])

    def _assemble(progress, cancel):
        logger.info("Step started: Cutting the film together")
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
            logger.info("Captions kept in film.srt: no caption font found")
        result["durations"] = list(state["durations"])
        result["total_seconds"] = cursor
        progress(1.0, "")
        logger.info("Step finished: Cutting the film together")

    steps = [
        (40, "Making pictures", _stills),
        (10, "Speaking the lines", _voice),
        (30, "Moving the pictures", _motion),
        (20, "Cutting the film together", _assemble),
    ]
    run_steps(steps, report=report, cancel_event=cancel_event)
    result["ended"] = time.time()
    logger.info("Film built in %.0f seconds",
                result["ended"] - result["started"])
    _save()
    return result
