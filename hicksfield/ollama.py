"""Talk to a local Ollama for the scene breakdown.

The idea goes in as plain words; the model answers with a JSON list of
scenes, each with a picture prompt and a line of narration. If Ollama is
not running, nothing is invented: the caller shows the plain message and
the user types scenes by hand.

Only the standard library is used. No API keys exist anywhere in this app.
"""

import json
import os
import socket
import urllib.error
import urllib.request

from hicksfield.errors import (
    CancelledError,
    HicksFieldError,
    OllamaUnreachableError,
)
from hicksfield.text import plural

DEFAULT_BASE_URL = "http://127.0.0.1:11434"

SYSTEM_PROMPT = (
    "You break a film idea into short scenes. Answer with a JSON list only, "
    "no other words. Each scene is an object with two strings: "
    "\"prompt\", a short visual description of one still picture for the "
    "scene, and \"narration\", one spoken line for the voiceover. "
    "Make 3 to 8 scenes. Keep each prompt under 30 words and each "
    "narration under 25 words."
)


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


def list_models(timeout=10):
    """Return the installed model names, e.g. ['minicpm5-heretic:2b']."""
    data = _request_json("GET", "/api/tags", timeout=timeout)
    models = data.get("models", [])
    return [m.get("name", "") for m in models if m.get("name")]


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


def break_into_scenes(idea, model, timeout=180):
    """Ask the local model to break `idea` into scenes.

    Returns (scenes, dropped, raw_text). Raises OllamaUnreachableError
    when Ollama is not answering.
    """
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": idea},
        ],
    }
    data = _request_json("POST", "/api/chat", payload, timeout=timeout)
    message = data.get("message", {})
    content = message.get("content", "")
    if not content:
        raise HicksFieldError(
            "The model answered with nothing.",
            "Try again, or type the scenes by hand below.",
        )
    return parse_scenes_reply(content)


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


def break_into_scenes_streaming(idea, model, on_scene, report=None,
                                cancel_event=None, timeout=180):
    """Ask the local model to break `idea` into scenes, streaming.

    Calls on_scene(scene) for each complete scene as it arrives, so
    the list fills in instead of appearing all at once. Returns
    (scenes, dropped, raw_text) like break_into_scenes. Raises
    CancelledError when cancel_event is set, OllamaUnreachableError
    when Ollama is not answering.

    The read timeout is short on purpose: a quiet stretch lets the
    loop check for cancel instead of hanging on a silent socket.
    """
    payload = {
        "model": model,
        "stream": True,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
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
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError()
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
        raise HicksFieldError(
            "The model answered, but none of the scenes had both a "
            "picture and a spoken line.",
            "Try again, or type the scenes by hand below.",
        )
    return scenes, dropped, raw
