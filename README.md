# HicksField

A free local film studio for Linux. Type an idea in plain words, and
HicksField turns it into a short film: scenes, still pictures, a spoken
voiceover, and a finished MP4. Everything runs on your own computer. No
account, no subscription, no cloud.

## What it does

1. **Idea.** Type what the film is about, in plain words.
2. **Scenes.** A local model breaks the idea into scenes, or you type the
   scenes yourself. Each scene gets a picture prompt and a line of narration.
3. **Stills.** One picture per scene, made on your computer by a small
   image model built for CPU.
4. **Motion.** The pictures move: slow push-ins and pans, cut together.
5. **Voice.** A local voice reads the narration out loud.
6. **Export.** The film is put together as an MP4, with captions.

## What it needs

- Linux, Python 3.10 or newer.
- ffmpeg, for motion, sound, and the finished MP4. If ffmpeg is missing,
  HicksField tells you how to install it.
- A stills engine, set up from inside the app the first time you make
  pictures. The fast one uses 1-step distilled image models. The steady
  one builds a small C++ image tool from source. Both are free.
- A voice, downloaded from inside the app the first time you add narration.
  About 60 MB.
- Ollama, only if you want the idea broken into scenes for you. Without it,
  you type the scenes yourself and everything else works the same.

## Install

Double-click the installer. It installs HicksField and everything it needs,
adds HicksField to the app menu, and checks the install works before it
reports success. No terminal needed.

To install from a terminal instead: `bash install.sh`

To run from a terminal after installing:
`bash ~/.local/share/hicksfield-app/run.sh`

## Tests

From the source folder, with PySide6 installed:

`python -m unittest discover -s tests -t .`

## What it does not do

- It does not generate moving video with AI. Real AI video needs a graphics
  card this PC does not have. The motion here comes from moving still
  pictures, which runs fine on CPU.
- It does not do lip sync. Same reason: it needs a graphics card.
- It does not call any paid service and has no place to put an API key.

## The look

HicksField looks like a small midnight cinema: dark room, warm projector
light, brass and deep red, film frames for scenes. The queue stays quiet
while it works, the buttons answer when pressed, and the finish gets a
small moment of its own.

## License

MIT. See LICENSE.
