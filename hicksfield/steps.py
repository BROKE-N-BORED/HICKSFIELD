"""Sequential step runner with progress and cancel.

Shared by engine setup and the film pipeline. Each step is a (weight,
label, function) tuple. The function gets a progress callback and the
cancel event. Progress inside a step is a 0..1 fraction, or None when the
step cannot measure its own progress (a pip install, a model load): the
caller then shows the step label with a busy light instead of a fake
percentage.
"""

from hicksfield.errors import CancelledError


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
