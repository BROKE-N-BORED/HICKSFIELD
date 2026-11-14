"""Small text helpers, shared by the windows and the workers.

One place for the words the app repeats, so the copy stays the same
everywhere it appears.
"""


def plural(count, singular, plural_form=None):
    """Count something in plain words: "1 scene" or "3 scenes".

    plural_form overrides the default "s" ending, for words like
    "spoken line" whose plural is "spoken lines" (the default works
    there too: "spoken line" + "s").
    """
    if count == 1:
        return f"{count} {singular}"
    if plural_form is None:
        plural_form = singular + "s"
    return f"{count} {plural_form}"
