"""Talk to a local Ollama for the scene breakdown.

The idea goes in as plain words; the model answers with a JSON list of
scenes, each with a picture prompt and a line of narration. If Ollama is
not running, nothing is invented: the caller shows the plain message and
the user types scenes by hand.

Only the standard library is used. No API keys exist anywhere in this app.
"""

import http.client
import json
import logging
import os
import queue
import socket
import threading
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


def model_fits(size_bytes, available_ram_mb=None):
    """Whether a model of `size_bytes` fits this PC.

    The model needs its own size in memory, and the rest of the
    computer still needs room to work, so a model fits when it takes
    at most half the memory that is actually free right now: judging
    against total memory would call a model a fit on a PC already
    running other things. Unknown size or unknown machine never
    blocks: it fits until proven otherwise.
    """
    if not size_bytes:
        return True
    if available_ram_mb is None:
        available_ram_mb = machine.mem_available_mb()
    if available_ram_mb <= 0:
        return True
    return size_bytes <= (available_ram_mb // 2) * 1024 * 1024


def model_can_chat(capabilities):
    """Whether a model can break an idea into scenes.

    A model that only completes text cannot chat. Unknown
    capabilities (an older Ollama, or a model Ollama cannot describe)
    count as able: the breakdown itself will say plainly if it cannot.
    """
    if not capabilities:
        return True
    return "chat" in capabilities


def _parameter_billions(parameter_size):
    """A model's size in billions of parameters, or -1 when unknown.

    Ollama reports shapes like "2.0B" or "751.63M": billions sort
    above millions, and an unknown shape sorts below every known
    one, so a model nobody can vouch for never beats one that is
    known good.
    """
    text = (parameter_size or "").strip().upper()
    if not text:
        return -1.0
    try:
        if text.endswith("B"):
            return float(text[:-1])
        if text.endswith("M"):
            return float(text[:-1]) / 1000.0
        return float(text)
    except ValueError:
        return -1.0


def _model_rank(info):
    """How good a model looks: more parameters first, then more
    bytes (a heavier quantisation of the same shape). Unknowns sort
    last on both counts."""
    size = info.get("size", 0) or 0
    return (_parameter_billions(info.get("parameter_size", "")),
            size if size > 0 else -1)


def recommended_model(infos, available_ram_mb=None):
    """The model to start with: the best one that fits this PC and
    can chat -- the most capable, not the smallest. None when no
    installed model qualifies."""
    fitting = [
        info for info in infos
        if model_fits(info.get("size", 0), available_ram_mb)
        and model_can_chat(info.get("capabilities", []))
    ]
    if not fitting:
        return None
    return max(fitting, key=_model_rank)


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

# How long the streaming loop waits for the next line before waking
# to check for cancel, deadlines, and the heartbeat: short enough
# that Cancel lands promptly, long enough not to spin.
STREAM_POLL_S = 0.5


def _response_socket(response):
    """The real socket under a urllib response, or None.

    Best effort: the socket hides two layers down (the response, the
    HTTP response, its buffered reader, the raw socket object), and
    a stand-in stream has none at all. The caller copes with None:
    the socket only buys a blocking read and a prompt shutdown,
    never correctness.
    """
    inners = (
        getattr(getattr(getattr(response, "fp", None), "fp", None),
                "raw", None),
        getattr(getattr(response, "fp", None), "raw", None),
    )
    for raw in inners:
        sock = getattr(raw, "_sock", None)
        if isinstance(sock, socket.socket):
            return sock
    return None


def _shutdown_socket(sock):
    """Shut a socket down, ignoring every failure: this only hurries
    a blocked reader thread home, and a socket that is already gone
    needs no hurrying."""
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _open_error(exc):
    """The plain-words error for a failed breakdown request.

    A refused connection means Ollama is not running. A timeout
    means it is running but still loading the model: saying "not
    running" then would send the user to restart a working server.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return HicksFieldError(
            f"Ollama answered with HTTP {exc.code}.",
            "Check Ollama is running and try again.",
        )
    reason = getattr(exc, "reason", exc)
    if isinstance(exc, socket.timeout) or isinstance(reason,
                                                     socket.timeout):
        return HicksFieldError(
            "Ollama is still loading the model.",
            "The model is taking a long time to load. Wait a little "
            "and try again, or try a smaller model.",
        )
    if isinstance(exc, (urllib.error.URLError, OSError, ValueError)):
        return OllamaUnreachableError()
    if isinstance(exc, http.client.HTTPException):
        return HicksFieldError(
            "Ollama answered, but the answer was not readable.",
            "Check Ollama is running and try again.",
        )
    return exc


def _stream_error(exc):
    """The plain-words error for a breakdown that dies mid-stream.

    A dropped connection reads like a dropped connection, never like
    a traceback: the raw socket error is logged for the debug report
    and never shown.
    """
    logger.info("Breakdown stream failed: %s: %s",
                type(exc).__name__, exc)
    if isinstance(exc, HicksFieldError):
        return exc
    if isinstance(exc, (ConnectionError, OSError,
                        http.client.HTTPException)):
        return HicksFieldError(
            "The connection to Ollama dropped mid-breakdown.",
            "Check Ollama is still running and try again. If it keeps "
            "dropping, try a smaller model, or type the scenes by "
            "hand below.",
        )
    return HicksFieldError(
        "The breakdown stopped unexpectedly.",
        "Try again, or type the scenes by hand below.",
    )


def _open_stream(request, silence_limit_s, report, cancel_event,
                 heartbeat_s, poll_s, started):
    """Open the breakdown request, waiting up to the silence limit
    for the first headers.

    The wait runs in a thread of its own: while Ollama loads the
    model, the loop can say so (every heartbeat, via report
    "loading") and can still be cancelled, instead of hanging on a
    silent socket. A model that is still loading is not a dead
    server, so a wait that runs out says plainly that the model is
    still loading.
    """
    opened = queue.Queue()

    def _opener():
        try:
            opened.put(("ok", urllib.request.urlopen(
                request, timeout=silence_limit_s)))
        except BaseException as exc:  # noqa: BLE001 - converted below
            opened.put(("error", exc))

    threading.Thread(target=_opener, daemon=True).start()
    last_loading = started
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError()
        now = time.monotonic()
        if now - started > silence_limit_s:
            logger.info("Breakdown failed: no answer from Ollama "
                        "after %d seconds", int(now - started))
            raise HicksFieldError(
                "Ollama is still loading the model.",
                "The model is taking a long time to load. Wait a "
                "little and try again, or try a smaller model.",
            )
        try:
            kind, value = opened.get(timeout=poll_s)
        except queue.Empty:
            if (report is not None
                    and now - last_loading > heartbeat_s):
                last_loading = now
                report("loading", None,
                       f"({int(now - started)}s in)")
            continue
        if kind == "error":
            raise _open_error(value)
        return value


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
    goes quiet for too long, is still loading when the silence limit
    runs out, or the breakdown runs past its total limit: a
    breakdown that never ends and never errors is worse than one
    that fails plainly.

    `scene_length` picks how many scenes to ask for ("short",
    "medium", "long"); the default asks for the usual 3 to 8.

    The socket is read by a thread of its own, so a quiet stretch
    is just an empty wait for the loop -- never a timeout on the
    socket itself, which would poison it. The loop wakes often
    enough to check for cancel, deadlines, and the heartbeat, and
    the wait for the first headers gets the same silence budget as
    the rest of the stream, reporting "loading" while the model is
    still loading. Connection trouble mid-stream is a plain-words
    error, never a raw socket error.
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
    started = time.monotonic()
    # How long each wait lasts before the loop wakes to check for
    # cancel, deadlines, and the heartbeat: short enough that Cancel
    # lands promptly, long enough not to spin.
    poll_s = max(0.05, min(timeout, heartbeat_s, STREAM_POLL_S))
    response = _open_stream(request, silence_limit_s, report,
                            cancel_event, heartbeat_s, poll_s, started)
    sock = _response_socket(response)
    if sock is not None:
        # A blocking socket never times out, so it can never be
        # poisoned by one: the silence limit and Cancel are the
        # loop's job now, not the socket's.
        try:
            sock.settimeout(None)
        except OSError:
            pass
    lines = queue.Queue()
    stop_reader = threading.Event()

    def _reader():
        # The only thread that ever touches the socket. A timeout
        # here is retryable, never fatal: a real socket is blocking
        # by now, so one can only come from a stream whose socket
        # could not be reached, and retrying is still the right
        # move. Anything else is queued for the loop, which says
        # plainly what happened.
        while not stop_reader.is_set():
            try:
                line = response.readline()
            except socket.timeout:
                stop_reader.wait(0.05)
                continue
            except BaseException as exc:  # noqa: BLE001 - queued
                if not stop_reader.is_set():
                    lines.put(exc)
                return
            if not line:
                lines.put(None)
                return
            lines.put(line)

    threading.Thread(target=_reader, daemon=True).start()
    scenes = []
    dropped = 0
    raw = ""
    cursor = 0
    # The clocks restart at the first headers: the wait for the
    # model to load had its own silence budget, and the stream gets
    # a fresh one.
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
                item = lines.get(timeout=poll_s)
            except queue.Empty:
                continue
            if item is None:
                break
            if isinstance(item, BaseException):
                raise _stream_error(item)
            line = item.strip()
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
        stop_reader.set()
        _shutdown_socket(sock)
        try:
            response.close()
        except Exception:  # noqa: BLE001 - closing must not mask the result
            pass
    if not scenes:
        logger.info("Breakdown finished with no usable scenes")
        raise HicksFieldError(
            "The model answered, but none of the scenes had both a "
            "picture and a spoken line.",
            "Try again, or type the scenes by hand below.",
        )
    logger.info("Breakdown finished: %d scenes", len(scenes))
    return scenes, dropped, raw
