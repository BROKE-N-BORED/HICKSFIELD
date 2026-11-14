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
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from hicksfield.errors import HicksFieldError

PROJECT_FILE = "project.json"
DEFAULT_SCENE_SECONDS = 5
STILLS_DIR = "stills"
CLIPS_DIR = "clips"
AUDIO_DIR = "audio"
FILM_FILE = "film.mp4"
CAPTIONS_FILE = "film.srt"


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Scene:
    prompt: str = ""
    narration: str = ""
    still: str = ""   # relative path, e.g. stills/scene-01.png
    clip: str = ""    # relative path, e.g. clips/scene-01.mp4
    audio: str = ""   # relative path, e.g. audio/scene-01.wav

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            return cls()
        return cls(
            prompt=str(data.get("prompt", "")),
            narration=str(data.get("narration", "")),
            still=str(data.get("still", "")),
            clip=str(data.get("clip", "")),
            audio=str(data.get("audio", "")),
        )


@dataclass
class Project:
    name: str = "Untitled film"
    idea: str = ""
    scenes: list = field(default_factory=list)
    created: str = ""
    updated: str = ""

    def to_dict(self):
        return {
            "name": self.name,
            "idea": self.idea,
            "scenes": [s.to_dict() for s in self.scenes],
            "created": self.created,
            "updated": self.updated,
        }

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise HicksFieldError("The project file is not a project.")
        scenes = [Scene.from_dict(s) for s in data.get("scenes", [])]
        return cls(
            name=str(data.get("name", "Untitled film")),
            idea=str(data.get("idea", "")),
            scenes=scenes,
            created=str(data.get("created", "")),
            updated=str(data.get("updated", "")),
        )


def scene_still_name(index):
    return f"scene-{index + 1:02d}.png"


def scene_clip_name(index):
    return f"scene-{index + 1:02d}.mp4"


def scene_audio_name(index):
    return f"scene-{index + 1:02d}.wav"


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
