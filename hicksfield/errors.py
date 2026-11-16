"""Errors with plain-words messages and fixes.

Every error the user can see carries two things: what went wrong, in plain
words, and what to do about it. Nothing here prints a traceback at the user.
"""


class HicksFieldError(Exception):
    """Base error. `hint` is the plain-words fix, shown under the message."""

    def __init__(self, message, hint=None):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def plain_text(self):
        if self.hint:
            return f"{self.message}\n{self.hint}"
        return self.message


class EngineNotReadyError(HicksFieldError):
    """A stills or voice engine is not set up yet."""


class FFmpegMissingError(HicksFieldError):
    """ffmpeg is not installed."""

    def __init__(self):
        super().__init__(
            "ffmpeg is not installed on this computer.",
            "Run the HicksField installer again. It installs ffmpeg "
            "for you.",
        )


class DownloadError(HicksFieldError):
    """A download failed."""


class OllamaUnreachableError(HicksFieldError):
    """Ollama is not answering on this computer."""

    def __init__(self):
        super().__init__(
            "Ollama is not running on this computer.",
            "Start Ollama to break the idea into scenes, or type your "
            "scenes below. Everything else works the same.",
        )


class CancelledError(HicksFieldError):
    """The user cancelled the job."""

    def __init__(self):
        super().__init__("Cancelled.")
