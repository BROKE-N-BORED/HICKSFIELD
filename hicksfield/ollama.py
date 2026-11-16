"""Talk to a local Ollama for the scene breakdown.

The idea goes in as plain words; the model answers with a JSON list of
scenes, each with a picture prompt and a line of narration. If Ollama is
not running, nothing is invented: the caller shows the plain message and
the user types scenes by hand.

Only the standard library is used. No API keys exist anywhere in this app.
"""

import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request

from hicksfield import machine
from hicksfield.errors import (
    CancelledError,
    HicksFieldError,
    OllamaUnreachableError,
)
from hicksfield.text import plural

logger = logging.getLogger("hicksfield.ollama")

DEFAULT_BASE_URL = "http://127.0.0.1:11434"

# How many scenes each film length asks the model for. Medium is
# today's range, kept as the default so nothing changes unless the
# user picks another length.
SCENE_LENGTHS = {
    "short": (3, 5),
    "medium": (3, 8),
    "long": (8, 12),
}
DEFAULT_SCENE_LENGTH = "medium"


def system_prompt(scene_length=DEFAULT_SCENE_LENGTH):
    """The breakdown instruction for a film length: the same words
    for every length, except the number of scenes it asks for. An
    unknown length falls back to the usual one."""
    min_scenes, max_scenes = SCENE_LENGTHS.get(
        scene_length, SCENE_LENGTHS[DEFAULT_SCENE_LENGTH])
    return (
        "You break a film idea into short scenes. Answer with a JSON "
        "list only, no other words. Each scene is an object with two "
        "strings: \"prompt\", a short visual description of one still "
        "picture for the scene, and \"narration\", one spoken line for "
        "the voiceover. "
        f"Make {min_scenes} to {max_scenes} scenes. Keep each prompt "
        "under 30 words and each narration under 25 words."
    )


SYSTEM_PROMPT = system_prompt()


def base_url():
    return os.environ.get("HICKSFIELD_OLLAMA_URL", DEFAULT_BASE_URL)


def _request_json(method, path, payload=None, timeout=10):
    url = base_url().rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise HicksFieldError(
            f"Ollama answered with HTTP {exc.code}.",
            "Check Ollama is running and try again.",
        ) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OllamaUnreachableError() from exc


def is_reachable(timeout=3):
    try:
        _request_json("GET", "/api/tags", timeout=timeout)
        return True
    except HicksFieldError:
        return False


def list_model_info(timeout=10):
    """Return installed models with size and shape.

    Each entry: {"name", "size" (bytes, 0 when unknown),
    "parameter_size" (e.g. "2.0B", "" when unknown)}.
    """
    data = _request_json("GET", "/api/tags", timeout=timeout)
    models = data.get("models", [])
    infos = []
    for entry in models:
        name = entry.get("name", "")
        if not name:
            continue
        details = entry.get("details", {}) or {}
        infos.append({
            "name": name,
            "size": entry.get("size", 0) or 0,
            "parameter_size": details.get("parameter_size", "") or "",
        })
    return infos


def list_models(timeout=10):
    """Return the installed model names, e.g. ['minicpm5-heretic:2b']."""
    return [info["name"] for info in list_model_info(timeout=timeout)]


def model_capabilities(name, timeout=5):
    """What one model can do, e.g. ["chat"], or [] when unknown.

    Best effort: an older Ollama, or a model Ollama cannot describe,
    answers without capabilities, and the caller treats that as
    unknown rather than incapable.
    """
    try:
        data = _request_json("POST", "/api/show", {"model": name},
                             timeout=timeout)
    except HicksFieldError:
        return []
    caps = data.get("capabilities", []) or []
    return [str(cap) for cap in caps if cap]


def model_fits(size_bytes, total_ram_mb=None):
    """Whether a model of `size_bytes` fits this PC.

    The model needs its own size in memory, and the rest of the
    computer still needs room to work, so a model fits when it takes
    at most half the computer's memory. Unknown size or unknown
    machine never blocks: it fits until proven otherwise.
    """
    if not size_bytes:
        return True
    if total_ram_mb is None:
        total_ram_mb = machine.mem_total_mb()
    if total_ram_mb <= 0:
        return True
    return size_bytes <= (total_ram_mb // 2) * 1024 * 1024


def model_can_chat(capabilities):
    """Whether a model can break an idea into scenes.

    A model that only completes text cannot chat. Unknown
    capabilities (an older Ollama, or a model Ollama cannot describe)
    count as able: the breakdown itself will say plainly if it cannot.
    """
    if not capabilities:
        return True
    return "chat" in capabilities


def recommended_model(infos, total_ram_mb=None):
    """The model to start with: the smallest one that fits this PC
    and can chat. None when no installed model qualifies."""
    fitting = [
        info for info in infos
        if model_fits(info.get("size", 0), total_ram_mb)
        and model_can_chat(info.get("capabilities", []))
    ]
    if not fitting:
        return None
    return min(fitting, key=lambda info: info.get("size", 0) or 0)


def parse_scenes_reply(text):
    """Parse a model reply into (scenes, dropped, raw_text).

    scenes is a list of {"prompt": str, "narration": str}. Entries missing
    either field are dropped and counted, never silently kept. Raises
    HicksFieldError when no JSON list can be found at all.
    """
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < 0 or end <= start:
        raise HicksFieldError(
            "The model did not answer with a list of scenes.",
            "Try again, or type the scenes by hand below.",
        )
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise HicksFieldError(
            "The model answered, but the scene list was not readable.",
            "Try again, or type the scenes by hand below.",
        ) from exc
    if not isinstance(parsed, list):
        raise HicksFieldError(
            "The model did not answer with a list of scenes.",
            "Try again, or type the scenes by hand below.",
        )
    scenes = []
    dropped = 0
    for entry in parsed:
        if not isinstance(entry, dict):
            dropped += 1
            continue
        prompt = str(entry.get("prompt", "")).strip()
        narration = str(entry.get("narration", "")).strip()
        if not prompt or not narration:
            dropped += 1
            continue
        scenes.append({"prompt": prompt, "narration": narration})
    if not scenes:
        raise HicksFieldError(
            "The model answered, but none of the scenes had both a picture "
            "and a spoken line.",
            "Try again, or type the scenes by hand below.",
        )
    return scenes, dropped, text


def _complete_objects(text, start):
    """Yield (end, obj) for each complete JSON object in `text`.

    Scenes are flat objects (prompt + narration strings, no nesting),
    but the scan is string-aware, so a brace inside a word does not
    close the object early. Incomplete trailing text is left for the
    next chunk: the caller keeps the cursor and calls again.
    """
    index = start
    length = len(text)
    while index < length:
        open_brace = text.find("{", index)
        if open_brace < 0:
            return
        depth = 0
        in_string = False
        escaped = False
        pos = open_brace
        closed = None
        while pos < length:
            char = text[pos]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    closed = pos
                    break
            pos += 1
        if closed is None:
            return
        try:
            obj = json.loads(text[open_brace:closed + 1])
        except json.JSONDecodeError:
            obj = None
        yield closed + 1, obj
        index = closed + 1


# Streaming breakdown limits. A model can take a while to load on a
# slow PC, so silence alone is not failure, but silence forever is:
# without these, a model that goes quiet spins the loop with no error
# and no end, and even Cancel can take minutes to land.
STREAM_READ_TIMEOUT_S = 10
STREAM_SILENCE_LIMIT_S = 120
STREAM_TOTAL_LIMIT_S = 600
STREAM_HEARTBEAT_S = 15


def break_into_scenes_streaming(idea, model, on_scene, report=None,
                                cancel_event=None,
                                timeout=STREAM_READ_TIMEOUT_S,
                                silence_limit_s=STREAM_SILENCE_LIMIT_S,
                                total_limit_s=STREAM_TOTAL_LIMIT_S,
                                heartbeat_s=STREAM_HEARTBEAT_S,
                                scene_length=DEFAULT_SCENE_LENGTH):
    """Ask the local model to break `idea` into scenes, streaming.

    Calls on_scene(scene) for each complete scene as it arrives, so
    the list fills in instead of appearing all at once. Returns
    (scenes, dropped, raw_text). Raises
    CancelledError when cancel_event is set, OllamaUnreachableError
    when Ollama is not answering, and HicksFieldError when the model
    goes quiet for too long or the breakdown runs past its total
    limit: a breakdown that never ends and never errors is worse
    than one that fails plainly.

    `scene_length` picks how many scenes to ask for ("short",
    "medium", "long"); the default asks for the usual 3 to 8.

    The read timeout is short on purpose: a quiet stretch lets the
    loop check for cancel, deadlines, and the heartbeat instead of
    hanging on a silent socket.
    """
    logger.info("Breaking idea into scenes with model %s", model)
    payload = {
        "model": model,
        "stream": True,
        "messages": [
            {"role": "system", "content": system_prompt(scene_length)},
            {"role": "user", "content": idea},
        ],
    }
    url = base_url().rstrip("/") + "/api/chat"
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise HicksFieldError(
            f"Ollama answered with HTTP {exc.code}.",
            "Check Ollama is running and try again.",
        ) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OllamaUnreachableError() from exc
    scenes = []
    dropped = 0
    raw = ""
    cursor = 0
    started = time.monotonic()
    last_content = started
    last_heartbeat = started
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError()
            now = time.monotonic()
            if now - last_content > silence_limit_s:
                logger.info("Breakdown failed: the model went quiet "
                            "after %d seconds without answering",
                            int(now - last_content))
                raise HicksFieldError(
                    "The model went quiet.",
                    "Ollama may still be loading the model, or the model "
                    "may be too big for this PC. Try a smaller model, or "
                    "type the scenes by hand below.",
                )
            if now - started > total_limit_s:
                logger.info("Breakdown failed: the breakdown ran past "
                            "its limit after %d seconds",
                            int(now - started))
                raise HicksFieldError(
                    "The breakdown took too long.",
                    "Try a smaller model, or type the scenes by hand "
                    "below.",
                )
            if now - last_heartbeat > heartbeat_s:
                last_heartbeat = now
                waiting = int(now - started)
                logger.info("Still waiting for the model (%d seconds, "
                            "%d scenes so far)", waiting, len(scenes))
                if report is not None:
                    if scenes:
                        detail = (f"{plural(len(scenes), 'scene')} so far, "
                                  f"still going ({waiting}s)")
                    else:
                        detail = ("waiting for the model to answer "
                                  f"({waiting}s in)")
                    report("Breaking the idea into scenes", None, detail)
            try:
                line = response.readline()
            except socket.timeout:
                continue
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                chunk = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if chunk.get("done"):
                break
            content = chunk.get("message", {}).get("content", "")
            if not content:
                continue
            raw += content
            last_content = time.monotonic()
            for end, obj in _complete_objects(raw, cursor):
                cursor = end
                if not isinstance(obj, dict):
                    dropped += 1
                    continue
                prompt = str(obj.get("prompt", "")).strip()
                narration = str(obj.get("narration", "")).strip()
                if not prompt or not narration:
                    dropped += 1
                    continue
                scene = {"prompt": prompt, "narration": narration}
                scenes.append(scene)
                if on_scene is not None:
                    on_scene(scene)
            if report is not None:
                report("Breaking the idea into scenes", None,
                       f"{plural(len(scenes), 'scene')} so far")
    finally:
        response.close()
    if not scenes:
        logger.info("Breakdown finished with no usable scenes")
        raise HicksFieldError(
            "The model answered, but none of the scenes had both a "
            "picture and a spoken line.",
            "Try again, or type the scenes by hand below.",
        )
    logger.info("Breakdown finished: %d scenes", len(scenes))
    return scenes, dropped, raw
