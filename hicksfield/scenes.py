"""The project: one film, plain files on disk.

A project is a folder with a project.json file and plain asset folders:
- stills/scene-01.png, one picture per scene
- clips/scene-01.mp4, one moving clip per scene
- audio/scene-01.wav, one spoken line per scene
- film.mp4, the finished film, plus film.srt captions

Paths in project.json are relative to the project folder, so a project
folder can be moved or copied and still works.
"""

import json
import os
import random
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from hicksfield.errors import HicksFieldError

PROJECT_FILE = "project.json"
# The project file format version. A project saved by this HicksField
# carries version 1; a project with a higher version was made by a
# newer HicksField and cannot be opened here.
PROJECT_FORMAT_VERSION = 1
DEFAULT_SCENE_SECONDS = 5
STILLS_DIR = "stills"
CLIPS_DIR = "clips"
AUDIO_DIR = "audio"
FILM_FILE = "film.mp4"
CAPTIONS_FILE = "film.srt"
# The voice every film used before voices could be picked: a spoken
# line with no recorded voice was spoken in this one.
DEFAULT_VOICE_ID = "en_US-lessac-medium"


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Scene:
    prompt: str = ""
    narration: str = ""
    still: str = ""   # relative path, e.g. stills/scene-01.png
    clip: str = ""    # relative path, e.g. clips/scene-01.mp4
    audio: str = ""   # relative path, e.g. audio/scene-01.wav
    # What each asset was made from. Empty means unknown (a scene
    # from before provenance was kept): the asset is kept as long as
    # its file exists, so old projects are never forced to remake
    # everything on their first run under a newer HicksField.
    still_prompt: str = ""
    voice_text: str = ""
    still_seed: int = 0  # 0 means no seed was recorded
    # The voice that spoke the line, e.g. en_US-lessac-medium.
    voice_id: str = ""
    # The camera move for this scene: "" means the film picks (the
    # move rotates by scene number), otherwise one of the moves in
    # video.MOTIONS, or "still" for no move at all.
    motion: str = ""
    # The move the clip was actually made with (the choice resolved:
    # "auto" becomes the move for the scene's number).
    clip_motion: str = ""
    # The scene's stable asset identity: the tag used in its asset
    # filenames (scene-<asset_id>.png / .mp4 / .wav). It travels with
    # the scene when scenes are reordered, so a remake after a reorder
    # writes to the scene's own file, never another scene's. Empty
    # means not assigned yet; ensure_asset_ids assigns one (the
    # scene's number when free, so a fresh film still gets
    # scene-01.png, scene-02.png, ...). Scenes from before this
    # field existed get their tag back from their recorded filenames
    # on load, so v0.3 positional files keep working untouched.
    asset_id: str = ""

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            return cls()
        try:
            seed = int(data.get("still_seed", 0) or 0)
        except (TypeError, ValueError):
            seed = 0
        asset_id = str(data.get("asset_id", "") or "")
        if not asset_id:
            # A scene from before asset ids existed: take the tag
            # back from its recorded filenames, so its v0.3 files
            # (scene-02.png, ...) stay its files.
            asset_id = _asset_id_from_paths(
                data.get("still", ""), data.get("clip", ""),
                data.get("audio", ""))
        return cls(
            prompt=str(data.get("prompt", "")),
            narration=str(data.get("narration", "")),
            still=str(data.get("still", "")),
            clip=str(data.get("clip", "")),
            audio=str(data.get("audio", "")),
            still_prompt=str(data.get("still_prompt", "")),
            voice_text=str(data.get("voice_text", "")),
            still_seed=seed,
            voice_id=str(data.get("voice_id", "")),
            motion=str(data.get("motion", "")),
            clip_motion=str(data.get("clip_motion", "")),
            asset_id=asset_id,
        )


_ASSET_NAME_RE = re.compile(r"^scene-([A-Za-z0-9]+)\.(png|mp4|wav)$")


def _asset_id_from_paths(*paths):
    """The asset tag hidden in recorded filenames, or "".

    A v0.3 scene recorded stills/scene-02.png: its tag is "02", so
    its files keep their names under the new scheme.
    """
    for path in paths:
        if not path:
            continue
        name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
        match = _ASSET_NAME_RE.match(name)
        if match:
            return match.group(1)
    return ""


def new_seed():
    """A fresh random seed for a picture: 1 .. 2**31 - 1."""
    return random.randint(1, 2**31 - 1)


def ensure_asset_ids(scenes):
    """Give every scene a stable, unique asset id, in place.

    Scenes that already have one keep it (duplicates are reassigned:
    two scenes must never share a tag, or they would share files).
    A scene without one takes its number ("01", "02", ...) when free
    — so a fresh film's files are still scene-01.png and friends —
    otherwise the next free number. Returns the scenes.
    """
    taken = set()
    for scene in scenes:
        if scene.asset_id:
            if scene.asset_id in taken:
                scene.asset_id = ""
            else:
                taken.add(scene.asset_id)
    for index, scene in enumerate(scenes):
        if scene.asset_id:
            continue
        candidate = f"{index + 1:02d}"
        if candidate in taken:
            number = 1
            while f"{number:02d}" in taken:
                number += 1
            candidate = f"{number:02d}"
        scene.asset_id = candidate
        taken.add(candidate)
    return scenes


def _asset_tag(scene, index=None):
    """The tag for a scene's filenames: its asset id, or its number
    when no id was assigned yet (callers assign ids first; this is
    the safety net, so a filename is never built from nothing)."""
    if getattr(scene, "asset_id", ""):
        return scene.asset_id
    if index is None:
        return "01"
    return f"{index + 1:02d}"


def scene_still_name_for(scene, index=None):
    """This scene's picture filename, stable across reorders."""
    return f"scene-{_asset_tag(scene, index)}.png"


def scene_clip_name_for(scene, index=None):
    """This scene's clip filename, stable across reorders."""
    return f"scene-{_asset_tag(scene, index)}.mp4"


def scene_audio_name_for(scene, index=None):
    """This scene's spoken-line filename, stable across reorders."""
    return f"scene-{_asset_tag(scene, index)}.wav"


def archive_previous_asset(path):
    """Keep the old asset before a remake overwrites it.

    Copies an existing file aside as a numbered version next to it
    (scene-01.v1.png, scene-01.v2.png, ...), so a remake that comes
    out worse never destroys the good one. Returns the archived Path,
    or None when there was nothing to keep.
    """
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return None
    version = 1
    while True:
        candidate = path.with_name(
            f"{path.stem}.v{version}{path.suffix}")
        if not candidate.exists():
            break
        version += 1
    shutil.copy2(path, candidate)
    return candidate


@dataclass
class Project:
    name: str = "Untitled film"
    idea: str = ""
    scenes: list = field(default_factory=list)
    created: str = ""
    updated: str = ""
    # The voice that reads the lines, e.g. en_US-lessac-medium.
    # The default is the voice every film used before voices could
    # be picked, so old projects keep sounding the same.
    voice: str = "en_US-lessac-medium"

    def to_dict(self):
        return {
            "version": PROJECT_FORMAT_VERSION,
            "name": self.name,
            "idea": self.idea,
            "scenes": [s.to_dict() for s in self.scenes],
            "created": self.created,
            "updated": self.updated,
            "voice": self.voice,
        }

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise HicksFieldError("The project file is not a project.")
        # No version means an old project, saved before versions
        # existed: it loads as version 1. A version newer than this
        # HicksField knows is refused plainly.
        try:
            version = int(data.get("version", 1))
        except (TypeError, ValueError):
            version = 1
        if version > PROJECT_FORMAT_VERSION:
            raise HicksFieldError(
                "This project was made with a newer HicksField.",
                "Update HicksField to open it.",
            )
        scenes = [Scene.from_dict(s) for s in data.get("scenes", [])]
        # Scenes with no recorded files (never built) take their
        # number as their tag now, so the tag is fixed from the first
        # load on and later reorders cannot move it.
        ensure_asset_ids(scenes)
        return cls(
            name=str(data.get("name", "Untitled film")),
            idea=str(data.get("idea", "")),
            scenes=scenes,
            created=str(data.get("created", "")),
            updated=str(data.get("updated", "")),
            voice=str(data.get("voice", "en_US-lessac-medium")),
        )


def scene_still_name(index):
    return f"scene-{index + 1:02d}.png"


def scene_clip_name(index):
    return f"scene-{index + 1:02d}.mp4"


def scene_audio_name(index):
    return f"scene-{index + 1:02d}.wav"


def _asset_missing(project_dir, relative):
    if not relative:
        return True
    path = Path(project_dir) / relative
    return not path.exists() or path.stat().st_size == 0


def still_is_stale(scene, project_dir):
    """True when the picture needs making: its file is missing, or it
    was made from a prompt that no longer matches the scene. A
    picture with no recorded prompt is only stale when missing."""
    if _asset_missing(project_dir, scene.still):
        return True
    return bool(scene.still_prompt) and scene.still_prompt != scene.prompt


def voice_is_stale(scene, project_dir, voice_id=None):
    """True when the spoken line needs making: its file is missing,
    or it was spoken from a line that no longer matches the scene,
    or (when `voice_id` is given) it was spoken in another voice. A
    line with no recorded text is only stale when missing; a line
    with no recorded voice counts as spoken in the usual voice, the
    only voice before voices could be picked, so it is out of date
    when the current voice is any other."""
    if _asset_missing(project_dir, scene.audio):
        return True
    if bool(scene.voice_text) and scene.voice_text != scene.narration:
        return True
    if not voice_id:
        return False
    recorded = scene.voice_id or DEFAULT_VOICE_ID
    return recorded != voice_id


def clip_is_stale(scene, project_dir, voice_id=None, motion=None):
    """True when the moving clip needs making: its file is missing,
    or it was made with another camera move (when `motion` is
    given), or its picture or spoken line is stale (a clip built
    from old pieces is old itself)."""
    if _asset_missing(project_dir, scene.clip):
        return True
    if motion and scene.clip_motion and scene.clip_motion != motion:
        return True
    return (still_is_stale(scene, project_dir)
            or voice_is_stale(scene, project_dir, voice_id))


def project_paths(project_dir):
    """Return the asset paths for a project folder."""
    project_dir = Path(project_dir)
    return {
        "file": project_dir / PROJECT_FILE,
        "stills": project_dir / STILLS_DIR,
        "clips": project_dir / CLIPS_DIR,
        "audio": project_dir / AUDIO_DIR,
        "film": project_dir / FILM_FILE,
        "captions": project_dir / CAPTIONS_FILE,
    }


def ensure_project_dirs(project_dir):
    paths = project_paths(project_dir)
    for key in ("stills", "clips", "audio"):
        paths[key].mkdir(parents=True, exist_ok=True)
    return paths


def save_project(project, project_dir):
    """Save project.json atomically (tmp file + replace)."""
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    ensure_project_dirs(project_dir)
    ensure_asset_ids(project.scenes)
    if not project.created:
        project.created = _utcnow()
    project.updated = _utcnow()
    paths = project_paths(project_dir)
    text = json.dumps(project.to_dict(), indent=2, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(project_dir), prefix=".project-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, paths["file"])
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return paths["file"]


def load_project(project_dir):
    """Load project.json. Raises HicksFieldError naming the file on failure."""
    paths = project_paths(project_dir)
    try:
        text = paths["file"].read_text(encoding="utf-8")
    except OSError as exc:
        raise HicksFieldError(
            f"Could not read the project file {paths['file']}.",
            "Check the folder exists and try again.",
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HicksFieldError(
            f"The project file {paths['file']} is not readable: {exc}.",
            "The file may be damaged. Open a backup or start a new film.",
        ) from exc
    return Project.from_dict(data)


def new_project(name, idea=""):
    return Project(name=name, idea=idea)
