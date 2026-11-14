# Changelog

## 0.2.0 (2026-11-14)

The UI redesign: every item on the audit task list, items 3-20.

- Home is a bento grid now: four glanceable tiles (Scenes, Pictures,
  Spoken lines, Film) that open their window when tapped, plus a
  first-run setup checklist (engine, voice, ffmpeg, Ollama) with a
  Hide this list button and a Help menu entry to bring it back.
- The machine strip lives in the footer: free memory and free disk,
  next to the working state.
- Scenes are film-frame rows: thumbnail, prompt, and Picture ready /
  Voice ready marks, with a green edge when both are ready. Three
  rows show at the default size. Drag to reorder, or Move up and
  Move down.
- Nothing is selected by default: no scene and no How to Use topic
  until you pick one, so Remove never aims at a scene you did not
  choose. The edit boxes are labeled Picture prompt and Spoken line.
- The breakdown streams in: scenes appear as they arrive, with a
  progress bar, a Cancel button that keeps the scenes made so far,
  and skeleton rows while it runs.
- Keyboard focus is visible everywhere (brass ring) with Tab order
  in visual order. Shortcuts: Ctrl+N new film, Ctrl+O open,
  Ctrl+S save, F1 How to Use, ? for the shortcuts list. Tooltips
  on the controls that needed explaining.
- The stills list shows three whole rows at its default size, and
  picking a picture shows it large in the preview pane.
- Every spoken line has a Play button, so a line can be heard
  before the film is built.
- The Export window plays the finished film in the app, with
  Play/Pause and a scrub slider.
- The recommended engine is marked: a Recommended badge on the
  engine card in Stills, and the recommended build button in
  Export. Fast stills is the one to start with; Steady is
  recommended when it is the only engine ready.
- Lists keep their scroll position when they refresh.
- Copy pass: counts read "1 scene" and "2 scenes", and the ffmpeg
  line reads "ffmpeg is ready (version 8.1.2)" instead of the
  copyright banner.
- Motion on/off in How to Use: with motion off, the finish moment
  appears at once instead of fading in.
- The idea box is three lines tall; the home layout fits its
  window with room to spare.

## 0.1.0 (2026-11-13)

First version. A free local film studio for Linux, built for an 8 GB
CPU-only PC.

- Idea to short film: scenes, stills, motion, voiceover, captions, MP4.
- Scene breakdown from a local model through Ollama, with plain manual
  scenes when Ollama is not running.
- Stills from 1-step distilled image models on CPU, with a steady C++
  image tool as the fallback engine. RAM check before generating.
- Narration from a local offline voice. Motion, assembly, captions, and
  the MP4 from ffmpeg.
- Job queue with real progress and cancel. Long work runs in worker
  threads; the window never blocks.
- Native Qt windows: one window per job, menu bar, How to Use index,
  single instance, window size remembered, debug report button.
- GUI double-click installer, app menu entry, and app icon.
