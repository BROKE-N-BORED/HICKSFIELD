# Changelog

## 0.5.0 (2026-11-17)

The update check: HicksField now checks for a new release when it
starts, quietly, and asks before it ever downloads anything.

- On launch, the app silently asks GitHub for the latest release.
  When you already have the newest version, nothing happens: no
  popup, no noise. When a newer release is out, it asks, and only
  your tap starts the download.
- The download is checked before anything is installed: the
  release names the commit it was built from, and the download
  must unpack into a folder carrying that commit. A download that
  does not match is refused in plain words and nothing is
  installed. Nothing is ever installed blind.
- The install replaces the app files in place. The venv is never
  touched, and your films, models, engines, and voices live
  outside the app folder, so an update never wipes them. After
  the install, the app asks whether to restart now or wait until
  next time.
- Help, Check for updates checks by hand and tells you the
  result either way. Help, Check for updates on startup turns
  the quiet startup check off and on.
- The About box and the project file now read the version from
  the same place, so they can no longer drift apart (the project
  file still said 0.2.0 while the app said 0.4.0).

## 0.4.0 (2026-11-16)

The full third task list (task list 3, items 1-48): the breakdown
fix that actually works this time, plus the recent-films crash,
Undo safety, silent scenes, the Export dead ends, and the rest.

- The breakdown no longer dies when the model goes quiet for more
  than 10 seconds. The read loop was reworked so a quiet stretch is
  just waiting: the 120-second silence limit and the "still
  waiting" heartbeat now work as designed, and a raw socket error
  is never shown.
- A model that is still loading is no longer reported as "Ollama is
  not running". The first answer gets the same waiting time as the
  rest of the stream, and the window says the model is still
  loading while it waits.
- A dropped connection mid-breakdown is reported plainly instead
  of showing a raw error.
- The recent-films menu no longer crashes on the second film or
  shows the folder path split into one-letter entries. It shows
  enough of the path to tell same-named films apart, and entries
  for deleted folders are dropped.
- Reordering scenes then remaking a picture can no longer overwrite
  a different scene's picture: each scene keeps its own picture,
  spoken line, and clip.
- Undo is safe now: it is cleared when the film changes, a snapshot
  is saved before every change (including adding and moving
  scenes, and replacing scenes with a breakdown), and the button
  greys out when there is nothing to undo.
- Edits typed in a scene are no longer thrown away when the film
  is switched.
- A scene with no spoken line stays silent instead of breaking the
  whole job, as the help already promised. An empty prompt is no
  longer sent to the picture engine.
- The home "Break into scenes" button now does what it says, and
  the Scenes Break button is disabled until a model is picked.
- The installer sets the executable bits on the installed scripts,
  so the first run does not depend on the repo's file modes; the
  README documents the right-click "Allow Launching" path for a
  fresh clone.
- The home tiles and Scenes rows count out-of-date pictures the
  same way Stills, Voice, and Export do.
- The Export "what is ready" summary refreshes as the build goes,
  instead of freezing at zero.
- The Scenes window shows the current idea, not the one it had when
  it opened.
- Old spoken lines go out of date when the voice changes, like new
  ones do, and switching voices warns first when spoken lines
  exist.
- Remake keeps the old picture instead of destroying it.
- The picture engine no longer silently reuses a previous run's
  picture when it makes nothing.
- Cancel now lands during long setup steps: the voice install, the
  picture download, and the engine download can all be stopped.
- Export says what is missing and where to fix it when no picture
  engine is set up, with a button that opens Stills. Pictures
  already ready are no longer blocked by an engine that isn't
  needed for them.
- The steady picture engine setup offers a one-click install for
  git and cmake instead of terminal commands.
- The installer looks like the app, has a Cancel button during the
  install, and the libEGL warning reads correctly.
- If the app cannot start, a message is shown instead of the menu
  icon silently doing nothing.
- The home checklist gives Ollama more time before marking it down.
- A silent film no longer needs a voice set up.
- First-made pictures record their seed, and a seed can be typed
  on remake to get a picture back.
- The Stills memory line refreshes live, like the home window's.
- Voice Play says plainly there is no speaker instead of hanging
  when the app closes.
- Checking models shows progress, and Refresh can't be double
  pressed into two checks.
- The recommended model is the best one that fits, not the
  smallest, and fit is judged on free memory, not total memory.
- The scenes list adds each arriving scene instead of rebuilding
  itself, and scene rows show their camera move.
- Stills caches thumbnails instead of reloading them from disk on
  every change.
- Build buttons explain on the button itself why they are grey.
- Empty lists carry a short hint saying what to do next.
- The picture engine download is verified, shows progress, and can
  be cancelled.
- The words pass: tooltips, help topics, the README, and error
  hints that were stale or wrong now say true things.
- The debug report has a Refresh button.
- After a build finishes, Export says how long it took and clears
  the step line.
- A failed preview frame says so instead of leaving a black box.
- A timeout says it timed out instead of reporting an error.
- The dead code is gone, and the film name and dates are shown
  instead of lying in the file.

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
