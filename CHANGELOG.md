# Changelog

## 0.3.0 (2026-11-15)

The full second task list (task list 2, items 1-18): the real-PC
failure fix (the model picker and the breakdown hang), plus stale
assets, autosave, undo, remake, voice choice, scene length, camera
moves, and the rest.

- The model picker now says what each model is: its size, whether it
  fits this PC, and whether it can chat. A recommended model is named
  when one fits. Picking a model that is too big, or one that cannot
  chat, warns in plain words and asks before trying it — it never
  refuses.
- The breakdown can no longer spin forever on a silent model. It
  fails plainly when the model goes quiet for too long or the
  breakdown runs past its total limit, and it reports "still
  waiting" while it waits, so the window never looks dead. Cancel
  answers within seconds now instead of up to three minutes.
- The installer now installs ffmpeg and the Python venv package too,
  asking for permission in a window instead of a terminal. If the
  permission step is declined, the rest of the install still goes
  ahead and the miss is reported as a warning at the end. The
  "install it with a terminal command" hints are gone: the error
  message and How to Use now point at the installer. The README
  says what the installer covers, truthfully.
- The Debug report's log is no longer empty during normal use. Jobs,
  build steps, kept assets, the scene breakdown (including its
  "still waiting" notes and quiet-model failures), picture making,
  speaking, and film cutting all write short plain lines to the
  debug log, so a stall leaves something to diagnose from.
- project.json now carries a version (1). Projects saved before
  versions existed still open; a project made with a newer
  HicksField is refused plainly instead of loading wrong.
- Voice downloads are now checked like the picture model: each file
  must arrive at its published size, and the voice model file is
  checked against its published checksum too, so a truncated
  download fails at download time instead of at speak time.
- Removed the unused non-streaming scene breakdown: the app only
  ever used the streaming version.
- Scene edits no longer vanish when another scene is picked. The
  prompt and spoken line are saved into the scene as soon as the
  selection changes, or the list refreshes, or the window closes.
  The Save this scene button still works the same way.
- A rebuild now remakes what changed. Each picture and spoken line
  remembers the words it was made from, so editing a prompt remakes
  just that picture, editing a line remakes just that line, and a
  clip is rebuilt when its picture or line is remade. Pictures and
  lines made before this are kept as they are. A picture or line
  made from older words is marked Out of date in the Stills and
  Voice lists, and Export counts out of date work as still to make.
- Stills has a Remake this picture button for the chosen scene. It
  makes a new picture with a new seed, and the seed is shown under
  the preview, so a good picture can be named and kept. Remakes
  after an edit keep the old seed instead, so the picture keeps its
  composition.
- Removing a scene, or saving an edit to one, can now be undone
  with the Undo button or Ctrl+Z. Undoing a remove puts the scene
  back where it was, with its picture and spoken line reconnected.
- The Make buttons now say when there is nothing to make. With
  every picture made, Stills says "All pictures are already made."
  and the button is off; Voice does the same for spoken lines.
- ffmpeg can now be installed from the Export window with one
  button, asking for permission in a window instead of a terminal.
  The Home checklist points there too. No terminal command for
  ffmpeg is printed anywhere in the app any more.
- The machine strip is live now: free memory and free disk refresh
  every few seconds while a job runs, and stop when the job ends.
- Home has one way in per window now. The three extra Stills, Voice,
  and Export buttons below the tiles are gone; the tiles open those
  windows, and the Break into scenes button stays.
- The Voice window offers three voices to pick from: Lessac, Ryan,
  and Alan, each with plain words about what it sounds like and how
  big its download is. Lessac is the recommended one, and the voice
  is kept per film. Changing the voice makes the spoken lines again
  in the new voice, and the moving clips with them.
- The breakdown offers Short, Medium, or Long for how many scenes
  the film gets. Short is 3 to 5 scenes and quickest to build,
  Medium is 3 to 8 scenes as before, Long is 8 to 12 scenes and
  takes longest.
- Each scene has its own camera move now: push in, pull out, pan
  left, pan right, still, or Auto, where the film picks a move for
  the scene. Changing a scene's move makes its moving clip again.
- The Export player shows the film's first frame while idle, so the
  window no longer sits black until Play. The frame is taken once
  per film and shows again when the film ends.

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
