"""Sequential step runner with progress and cancel.

Shared by engine setup and the film pipeline. Each step is a (weight,
label, function) tuple. The function gets a progress callback and the
cancel event. Progress inside a step is a 0..1 fraction, or None when the
step cannot measure its own progress (a pip install, a model load): the
caller then shows the step label with a busy light instead of a fake
percentage.

run_logged lives here too: the cancellable way to run one long child
process (a pip install, a git clone, a picture generation). It polls
the cancel event while the child runs, so Cancel lands mid-step
instead of waiting for the step to finish.
"""

import subprocess
import tempfile
import time

from hicksfield.errors import CancelledError

# The tail run_logged returns when it kills a child for running past
# its time limit. Callers test for it with run_timed_out, so a timeout
# is reported as a timeout, not as a generic error.
RUN_TIMEOUT_TAIL = "the command ran past its time limit"


def _tail_lines(text, count=12):
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-count:]


def run_logged(cmd, cwd=None, env=None, timeout_s=1800,
               cancel_event=None):
    """Run a long command with output to a temp file, not a pipe.

    A pipe nobody reads fills up and deadlocks a long run. The temp file
    keeps the last lines for the failure message. Returns (returncode,
    tail_text). Returns (None, RUN_TIMEOUT_TAIL) when the command ran
    past its time limit, and (None, "could not start...") when the
    command could not start at all. Raises CancelledError when the
    cancel event is set (the child is killed first).
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
                return None, RUN_TIMEOUT_TAIL
            time.sleep(0.2)
        log.seek(0)
        return proc.returncode, "\n".join(_tail_lines(log.read()))
    finally:
        log.close()


def run_timed_out(code, tail):
    """True when a run_logged result means "it timed out"."""
    return code is None and tail == RUN_TIMEOUT_TAIL


def run_steps(steps, report=None, cancel_event=None):
    """Run steps in order. Returns a list of step results.

    report(label, overall_fraction_or_None, detail) is called when a step
    starts and whenever it reports progress. overall_fraction is None
    while a step reports indeterminate progress.
    """
    total_weight = sum(weight for weight, _label, _fn in steps)
    if total_weight <= 0:
        total_weight = 1
    done_weight = 0
    results = []
    for weight, label, func in steps:
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError()
        base = done_weight / total_weight
        span = weight / total_weight

        def step_progress(fraction, detail="", _base=base, _span=span,
                          _label=label):
            if report is None:
                return
            if fraction is None:
                report(_label, None, detail)
            else:
                report(_label, _base + _span * max(0.0, min(1.0, fraction)),
                       detail)

        if report is not None:
            report(label, base, "")
        results.append(func(step_progress, cancel_event))
        done_weight += weight
        if report is not None:
            report(label, done_weight / total_weight, "")
    return results
