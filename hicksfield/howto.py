"""The How to Use index: plain words, one topic at a time.

Every topic says what to do and what happens next. No jargon, no
em-dashes, no bare ampersands: this text shows on screen.
"""

HOWTO_TOPICS = [
    (
        "Start here",
        "HicksField turns an idea into a short film, on your own computer. "
        "Type the idea, break it into scenes, make a picture for each "
        "scene, let the voice read the lines, and build the film. Each job "
        "has its own window. Open them from the Windows menu.\n\n"
        "Nothing leaves your computer. There is no account and no cost.",
    ),
    (
        "Your idea",
        "On the main window, type what the film is about in plain words. "
        "One or two sentences is enough. Example: a street musician plays "
        "violin under a train bridge at dusk, and a train rumbles past "
        "overhead.\n\n"
        "Then press Break into scenes. If Ollama is running, a local model "
        "turns the idea into scenes. If not, type the scenes yourself in "
        "the Scenes window. Everything else works the same either way.",
    ),
    (
        "Scenes",
        "A scene is one picture and one spoken line. The picture prompt "
        "says what the picture shows. The narration is what the voice "
        "says over that picture.\n\n"
        "You can add a scene, change a scene, or remove one. Removing a "
        "scene asks first. A scene with no picture prompt cannot make a "
        "picture, and a scene with no spoken line stays silent.",
    ),
    (
        "Stills",
        "The Stills window makes one picture per scene. Two engines can "
        "make pictures. The fast one is quick, a few seconds a picture. "
        "The steady one is slower, minutes a picture, but simple.\n\n"
        "The first time, press Set up next to the engine you want. Setup "
        "downloads what it needs and shows real progress. You can cancel "
        "and pick up where it left off. Pictures need free RAM, so "
        "HicksField checks before it starts and says so plainly if RAM "
        "is short.",
    ),
    (
        "Voice",
        "The Voice window gives the film its spoken lines. The first time, "
        "press Set up the voice. It downloads one voice, about 60 MB.\n\n"
        "Press Make the spoken lines to speak every scene's line. A line "
        "that is already spoken is kept, not made again.",
    ),
    (
        "Export",
        "The Export window builds the finished film. It makes any missing "
        "pictures, spoken lines, and moving clips first, then cuts them "
        "together into film.mp4 with captions in film.srt.\n\n"
        "The build shows real progress and can be cancelled. Cancelling "
        "keeps everything made so far, and the next build picks up from "
        "there. When the film is ready, press Open the film to watch it.",
    ),
    (
        "Engines and downloads",
        "Engines and voices live in the app data folder on this computer, "
        "not in the film folders. Downloads resume after a failure or a "
        "freeze. A download that finishes is checked against its size, "
        "and the picture model is checked against its checksum too.\n\n"
        "Only one big job runs at a time, so the desktop stays usable "
        "while a job runs.",
    ),
    (
        "Keyboard shortcuts",
        "Ctrl+N starts a new film. Ctrl+O opens a film. Ctrl+S saves. "
        "F1 opens this How to Use window. Press ? anywhere to see the "
        "list again.\n\n"
        "Tab moves between buttons and boxes. Space presses the focused "
        "button. The focused button shows a brass ring.",
    ),
    (
        "If something goes wrong",
        "The lamp in the corner says what the app is doing: grey is idle, "
        "brass is working, green is done, red means something went wrong. "
        "The words next to it say what happened and what to do.\n\n"
        "If ffmpeg is missing, install it with: sudo apt install ffmpeg\n\n"
        "The Debug report button in the Help menu copies a report about "
        "what the app was doing. It holds no secrets.",
    ),
]


def topic_titles():
    return [title for title, _text in HOWTO_TOPICS]


def topic_text(title):
    for topic_title, text in HOWTO_TOPICS:
        if topic_title == title:
            return text
    return ""
