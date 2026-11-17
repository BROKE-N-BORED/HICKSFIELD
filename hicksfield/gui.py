"""The windows: one function, one window.

Main window is the hub: the idea and the way in. Scenes, Stills, Voice
and Export each have their own window. Long work runs in worker threads;
the window that started it shows real progress and a cancel button.
Only one heavy job runs at a time across the whole app.
"""

import concurrent.futures
import copy
import json
import logging
import random
import re
import sys
import time
import weakref
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QSettings, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:
    from PySide6.QtMultimedia import (
        QAudioOutput, QMediaDevices, QMediaPlayer)
    from PySide6.QtMultimediaWidgets import QVideoWidget
    HAVE_MULTIMEDIA = True
except ImportError:
    HAVE_MULTIMEDIA = False
    QAudioOutput = None
    QMediaDevices = None
    QMediaPlayer = None
    QVideoWidget = None

from PySide6.QtCore import Signal

from hicksfield import (
    __version__,
    debuglog,
    machine,
    ollama,
    paths,
    scenes as scenes_mod,
    stills,
    sysdeps,
    video,
    voice,
)
from hicksfield.downloader import format_bytes
from hicksfield.errors import (
    CancelledError,
    HicksFieldError,
    OllamaUnreachableError,
)
from hicksfield.howto import HOWTO_TOPICS
from hicksfield.pipeline import build_film
from hicksfield.single import SingleInstance
from hicksfield.stills import RamShortError
from hicksfield.style import (
    BRASS_DARK,
    PANEL_LIGHT,
    DipButton,
    FilmStrip,
    FinishBadge,
    StatusLamp,
    apply_style,
    set_motion_enabled,
)
from hicksfield.text import plural
from hicksfield.workers import JobLock, Worker

ORG_NAME = "BrokeNBored"
APP_NAME = "HicksField"
MAX_RECENT = 5


def _weak_slot(obj, method_name):
    """A signal slot that does not keep its window alive.

    A plain bound-method connection is held by the sender's C++ side,
    which Python's garbage collector cannot see: the window, the
    shared state, and the connection form a cycle that is never
    collected, so closed windows pile up until interpreter shutdown
    (where destroying them crashes). The weak reference breaks the
    cycle; a dead window's slot is a harmless no-op.
    """
    ref = weakref.ref(obj)

    def _slot(*args, **kwargs):
        target = ref()
        if target is not None:
            getattr(target, method_name)(*args, **kwargs)

    return _slot


def settings():
    return QSettings(ORG_NAME, APP_NAME)


def _load_recent():
    """The recent-films list from settings, always a list.

    QSettings stores a 1-item list as a bare string, which reads back
    as a `str`: iterating it yields characters and `insert` crashes.
    A lone string is wrapped; anything that is not a list is dropped.
    """
    value = settings().value("recent", []) or []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


def _recent_label(path):
    """Enough of the path to tell same-named films apart: the film
    folder plus its parent, e.g. "Films/dusk"."""
    folder = Path(path)
    parent = folder.parent.name
    if parent:
        return f"{parent}/{folder.name}"
    return folder.name


def _ollama_reachable():
    """Is Ollama up? A 10 s timeout, retried once: a busy Ollama can
    miss a single short check, and the checklist must not cry wolf
    on a slow PC."""
    return ollama.is_reachable(10) or ollama.is_reachable(10)


def slugify(name):
    text = re.sub(r"[^a-z0-9]+", "-", name.strip().lower())
    return text.strip("-") or "film"


class AppState(QObject):
    """The shared state: the current film and the one-job lock."""

    project_changed = Signal()
    job_changed = Signal(object)
    # A window asking the main window to open another window, by key
    # ("stills", "voice", ...): windows do not know about each other.
    open_window_requested = Signal(str)

    def __init__(self):
        super().__init__()
        self.project = scenes_mod.Project(idea="", scenes=[])
        self.project_dir = None
        self.job_lock = JobLock()

    def new_project(self, name):
        base = paths.projects_dir()
        base.mkdir(parents=True, exist_ok=True)
        slug = slugify(name)
        target = base / slug
        suffix = 2
        while target.exists():
            target = base / f"{slug}-{suffix}"
            suffix += 1
        self.project = scenes_mod.new_project(name)
        self.project_dir = target
        scenes_mod.ensure_project_dirs(target)
        self.save()
        self.project_changed.emit()
        return target

    def open_project(self, project_dir):
        project_dir = Path(project_dir)
        project_file = project_dir / "project.json"
        if not project_file.exists():
            raise HicksFieldError(
                "That folder is not a HicksField film.",
                "A film folder holds a project.json file.",
            )
        self.project = scenes_mod.load_project(project_dir)
        self.project_dir = project_dir
        scenes_mod.ensure_project_dirs(project_dir)
        self.project_changed.emit()
        return project_dir

    def save(self, _project=None):
        """Save the current project. The optional argument exists so
        this method can be passed straight to pipeline.build_film as
        its save_cb, which calls save_cb(project) after each piece
        lands; the state always saves its own project. Saving
        announces the change, so every window refreshes from the
        saved state: retyping the idea on Main reaches the Scenes
        echo, and a build's progress reaches the Export summary."""
        if self.project_dir is None:
            return
        scenes_mod.save_project(self.project, self.project_dir)
        self.project_changed.emit()

    def try_job(self, owner):
        if self.job_lock.try_start(owner):
            self.job_changed.emit(owner)
            return True
        return False

    def finish_job(self):
        self.job_lock.finish()
        self.job_changed.emit(None)

    @property
    def job_owner(self):
        return self.job_lock.owner

    def project_name(self):
        if self.project_dir is None:
            return "No film yet"
        # The name the film was given. A film saved before names were
        # kept still says "Untitled film": show its folder name
        # instead, which is the name the user can see on disk.
        name = self.project.name.strip()
        if name and name.lower() != "untitled film":
            return name
        return self.project_dir.name


class ModeWindow(QWidget):
    """Base for the one-function windows: header, film strip, status."""

    def __init__(self, state, title, key, width=900, height=600):
        super().__init__(None, Qt.Window)
        self.state = state
        self.setWindowTitle(f"{title} - HicksField")
        self._settings_key = key
        self._default_size = (width, height)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(16, 14, 16, 14)
        self.layout.setSpacing(10)
        title_label = QLabel(title)
        title_label.setProperty("role", "title")
        self.layout.addWidget(title_label)
        self.layout.addWidget(FilmStrip())
        status_row = QHBoxLayout()
        self.lamp = StatusLamp()
        self.status_label = QLabel("Ready.")
        self.status_label.setProperty("role", "muted")
        self.status_label.setWordWrap(True)
        status_row.addWidget(self.lamp)
        status_row.addWidget(self.status_label, 1)
        self._status_row = status_row
        self._restore_geometry()

    def finish_layout(self):
        self.layout.addLayout(self._status_row)

    def _restore_geometry(self):
        saved = settings().value(f"geometry/{self._settings_key}")
        self.resize(*self._default_size)
        if saved is not None:
            self.restoreGeometry(saved)
        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            frame = self.frameGeometry()
            if not available.contains(frame.topLeft()):
                self.move(available.topLeft())
            if frame.width() > available.width():
                self.resize(available.width(), self.height())
            if frame.height() > available.height():
                self.resize(self.width(), available.height())

    def closeEvent(self, event):
        settings().setValue(f"geometry/{self._settings_key}",
                            self.saveGeometry())
        worker = getattr(self, "_worker", None)
        if worker is not None:
            worker.cancel()
        super().closeEvent(event)

    def show_status(self, lamp_state, text):
        self.lamp.set_state(lamp_state)
        self.status_label.setText(text)

    def ask_yes_no(self, title, text):
        """A yes/no question that defaults to No."""
        answer = QMessageBox.question(
            self, title, text,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    def show_plain_error(self, exc):
        if isinstance(exc, HicksFieldError):
            message = exc.plain_text()
        else:
            message = str(exc)
            logging.getLogger("hicksfield.gui").exception(
                "unexpected error: %s", exc)
        self.show_status("error", message)


class HowToWindow(ModeWindow):
    def __init__(self, state):
        super().__init__(state, "How to Use", "howto", 800, 520)
        splitter = QSplitter(Qt.Horizontal)
        self.topic_list = QListWidget()
        for title, _text in HOWTO_TOPICS:
            self.topic_list.addItem(title)
        self.topic_list.currentRowChanged.connect(self._show_topic)
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        splitter.addWidget(self.topic_list)
        splitter.addWidget(self.text)
        splitter.setSizes([200, 560])
        self.layout.addWidget(splitter, 1)
        motion_row = QHBoxLayout()
        self.motion_check = QCheckBox("Motion")
        self.motion_check.setChecked(
            settings().value("motion/enabled", 1, type=int) == 1)
        self.motion_check.setToolTip(
            "Play the finish animation when a film is ready.")
        self.motion_check.toggled.connect(self._toggle_motion)
        motion_row.addWidget(self.motion_check)
        motion_note = QLabel(
            "Play the finish animation when a film is ready. "
            "Turn it off for a still screen.")
        motion_note.setProperty("role", "muted")
        motion_note.setWordWrap(True)
        motion_row.addWidget(motion_note, 1)
        self.layout.addLayout(motion_row)
        self.finish_layout()
        self.text.setPlainText("Pick a topic on the left to read it.")
        # The reading pane takes the first focus, so the topic list
        # does not select its first row just by being shown.
        self.text.setFocus()

    def _toggle_motion(self, checked):
        settings().setValue("motion/enabled", 1 if checked else 0)
        set_motion_enabled(checked)
        self.show_status("idle", "Motion on." if checked
                         else "Motion off.")

    def _show_topic(self, row):
        if 0 <= row < len(HOWTO_TOPICS):
            self.text.setPlainText(HOWTO_TOPICS[row][1])
            self.show_status("idle", HOWTO_TOPICS[row][0] + ".")
        else:
            self.text.setPlainText("Pick a topic on the left to read it.")


class DebugReportWindow(ModeWindow):
    def __init__(self, state):
        super().__init__(state, "Debug Report", "debug", 720, 520)
        note = QLabel(
            "This report says what the app was doing. It holds no secrets."
        )
        note.setProperty("role", "muted")
        note.setWordWrap(True)
        self.layout.addWidget(note)
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self._load_report()
        self.layout.addWidget(self.text, 1)
        row = QHBoxLayout()
        refresh_button = DipButton("Refresh")
        refresh_button.setToolTip(
            "Build the report again, from what the app is doing now.")
        refresh_button.clicked.connect(self._refresh_report)
        copy_button = DipButton("Copy the report")
        copy_button.clicked.connect(self._copy)
        close_button = DipButton("Close")
        close_button.clicked.connect(self.close)
        row.addStretch(1)
        row.addWidget(refresh_button)
        row.addWidget(copy_button)
        row.addWidget(close_button)
        self.layout.addLayout(row)
        self.finish_layout()

    def _load_report(self):
        self.text.setPlainText(debuglog.build_debug_report(
            __version__, machine.machine_summary()))

    def _refresh_report(self):
        self._load_report()
        self.show_status("done", "Refreshed.")

    def _copy(self):
        QApplication.clipboard().setText(self.text.toPlainText())
        self.show_status("done", "Copied.")


def _short_date(iso_text):
    """An ISO timestamp as a short date: "16 Nov 2026". Empty when
    the text is blank or not a date."""
    if not iso_text:
        return ""
    try:
        moment = datetime.fromisoformat(str(iso_text))
    except ValueError:
        return ""
    return moment.strftime("%d %b %Y")


def film_dates_text(project):
    """The film's dates in plain words: "Started 16 Nov 2026. Last
    saved 17 Nov 2026." Empty when the film has no dates yet."""
    started = _short_date(project.created)
    saved = _short_date(project.updated)
    if started and saved and started != saved:
        return f"Started {started}. Last saved {saved}."
    if started:
        return f"Started {started}."
    if saved:
        return f"Last saved {saved}."
    return ""


def _gb_text(mb):
    """A size in plain words: "3 GB" or "512 MB"."""
    if mb >= 1024:
        return f"{mb // 1024} GB"
    return f"{mb} MB"


def _format_clock(seconds):
    """Seconds as a clock: "1:05"."""
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}:{secs:02d}"


class TileButton(QPushButton):
    """A glanceable tile: one big fact, one name, one short line.

    A real button, so it takes keyboard focus and answers to Space
    and Enter like any other button. The bento home is four of these.
    """

    def __init__(self, name, parent=None):
        super().__init__(parent)
        self.setProperty("role", "tile")
        self.setCursor(Qt.PointingHandCursor)
        layout = QVBoxLayout(self)
        # The inset lives here, not in the stylesheet: QSS padding
        # does not inset a child layout, so the tile rule's padding
        # would leave the labels on the very edge.
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(2)
        self.number_label = QLabel()
        self.number_label.setProperty("role", "tile-number")
        self.number_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout.addWidget(self.number_label)
        name_label = QLabel(name)
        name_label.setProperty("role", "section")
        name_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout.addWidget(name_label)
        self.sub_label = QLabel()
        self.sub_label.setProperty("role", "tile-label")
        self.sub_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.sub_label.setWordWrap(True)
        layout.addWidget(self.sub_label)

    def sizeHint(self):
        # QPushButton sizes itself from its own text, which is empty
        # here: the labels live in a child layout the button knows
        # nothing about. Without this the tile is crushed to one
        # line tall and the labels pile up on each other.
        hint = self.layout().totalSizeHint()
        margins = self.contentsMargins()
        return QSize(hint.width() + margins.left() + margins.right(),
                     hint.height() + margins.top() + margins.bottom())

    def minimumSizeHint(self):
        return self.sizeHint()

    def set_fact(self, number, sub):
        self.number_label.setText(str(number))
        self.sub_label.setText(sub)


class MainWindow(QMainWindow):
    DEFAULT_WIDTH = 1100
    DEFAULT_HEIGHT = 700

    def __init__(self, state):
        super().__init__()
        self.state = state
        self.setWindowTitle("HicksField")
        self.resize(self.DEFAULT_WIDTH, self.DEFAULT_HEIGHT)
        saved = settings().value("geometry/main")
        if saved is not None:
            self.restoreGeometry(saved)
        self._windows = {}
        self._idea_timer = QTimer(self)
        self._idea_timer.setSingleShot(True)
        self._idea_timer.setInterval(500)
        self._idea_timer.timeout.connect(self._save_idea)
        # The machine strip refreshes while a job runs, so the free
        # memory and disk numbers move as the job works. The timer
        # only runs while a job is running: ticking forever while
        # idle would wake the computer for nothing.
        self._machine_timer = QTimer(self)
        self._machine_timer.setInterval(5000)
        self._machine_timer.timeout.connect(self._refresh_machine)
        # What the idea box last showed for which film: the refresh
        # must not clobber typing that has not been saved yet.
        self._saved_idea = None
        self._idea_project_dir = None
        self._ollama_ok = None
        self._ollama_worker = None
        self._checklist_dismissed = (
            settings().value("checklist/dismissed", 0, type=int) == 1)
        self._build_menus()
        self._build_central()
        state.project_changed.connect(_weak_slot(self, "_refresh"))
        state.open_window_requested.connect(
            _weak_slot(self, "open_window"))
        state.job_changed.connect(_weak_slot(self, "_on_job_changed"))
        self._refresh()
        self._check_ollama()

    def _build_menus(self):
        bar = self.menuBar()
        file_menu = bar.addMenu("File")
        new_action = QAction("New film", self)
        new_action.setShortcut(QKeySequence("Ctrl+N"))
        new_action.setShortcutContext(Qt.ApplicationShortcut)
        new_action.triggered.connect(self._new_project)
        file_menu.addAction(new_action)
        open_action = QAction("Open film", self)
        open_action.setShortcut(QKeySequence("Ctrl+O"))
        open_action.setShortcutContext(Qt.ApplicationShortcut)
        open_action.triggered.connect(self._open_project)
        file_menu.addAction(open_action)
        save_action = QAction("Save", self)
        save_action.setShortcut(QKeySequence("Ctrl+S"))
        save_action.setShortcutContext(Qt.ApplicationShortcut)
        save_action.triggered.connect(self._save_project)
        file_menu.addAction(save_action)
        file_menu.addSeparator()
        self._recent_menu = file_menu.addMenu("Recent films")
        file_menu.addSeparator()
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        windows_menu = bar.addMenu("Windows")
        for title, key in (("Scenes", "scenes"), ("Stills", "stills"),
                           ("Voice", "voice"), ("Export", "export")):
            action = QAction(title, self)
            action.triggered.connect(lambda _c, k=key: self.open_window(k))
            windows_menu.addAction(action)

        help_menu = bar.addMenu("Help")
        howto_action = QAction("How to Use", self)
        howto_action.setShortcut(QKeySequence("F1"))
        howto_action.setShortcutContext(Qt.ApplicationShortcut)
        howto_action.triggered.connect(lambda: self.open_window("howto"))
        help_menu.addAction(howto_action)
        checklist_action = QAction("Setup checklist", self)
        checklist_action.triggered.connect(self._show_checklist)
        help_menu.addAction(checklist_action)
        shortcuts_action = QAction("Keyboard shortcuts", self)
        shortcuts_action.triggered.connect(self._show_shortcuts)
        help_menu.addAction(shortcuts_action)
        debug_action = QAction("Debug report", self)
        debug_action.triggered.connect(lambda: self.open_window("debug"))
        help_menu.addAction(debug_action)
        about_action = QAction("About HicksField", self)
        about_action.triggered.connect(self._about)
        help_menu.addAction(about_action)

        # "?" opens the shortcuts list, unless the user is typing in a
        # text box, where "?" is just a character.
        question = QShortcut(QKeySequence(Qt.Key_Question), self)
        question.setContext(Qt.ApplicationShortcut)
        question.activated.connect(self._maybe_show_shortcuts)

    def _maybe_show_shortcuts(self):
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit, QTextEdit, QPlainTextEdit,
                              QComboBox)):
            return
        self._show_shortcuts()

    def _shortcuts_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Keyboard shortcuts - HicksField")
        layout = QVBoxLayout(dialog)
        title = QLabel("Keyboard shortcuts")
        title.setProperty("role", "title")
        layout.addWidget(title)
        text = QLabel(
            "Ctrl+N  New film\n"
            "Ctrl+O  Open film\n"
            "Ctrl+S  Save\n"
            "Ctrl+Z  Undo the last scene change (in Scenes)\n"
            "F1  How to Use\n"
            "?  This list\n\n"
            "Tab moves between buttons and boxes. "
            "Space presses the focused button.")
        text.setWordWrap(True)
        layout.addWidget(text)
        close_button = DipButton("Close")
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button)
        return dialog

    def _show_shortcuts(self):
        self._shortcuts_dialog().exec()

    def _build_central(self):
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 12, 20, 12)
        layout.setSpacing(8)

        title = QLabel("HicksField")
        title.setProperty("role", "title")
        layout.addWidget(title)
        subtitle = QLabel("A free local film studio. Your idea in, "
                          "a short film out.")
        subtitle.setProperty("role", "subtitle")
        layout.addWidget(subtitle)
        layout.addWidget(FilmStrip())

        self.project_label = QLabel()
        self.project_label.setProperty("role", "section")
        layout.addWidget(self.project_label)
        self.project_dates_label = QLabel()
        self.project_dates_label.setProperty("role", "muted")
        layout.addWidget(self.project_dates_label)

        hero = QFrame()
        hero.setProperty("role", "card")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(14, 12, 14, 12)
        idea_label = QLabel("What is the film about?")
        idea_label.setProperty("role", "section")
        hero_layout.addWidget(idea_label)
        self.idea_edit = QTextEdit()
        self.idea_edit.setPlaceholderText(
            "Type the idea in plain words. One or two sentences is enough."
        )
        # Three lines is enough for one or two sentences, and the
        # home layout needs the room: any taller and the checklist
        # buttons below get squeezed.
        self.idea_edit.setMaximumHeight(64)
        self.idea_edit.setToolTip(
            "Type the film idea here. It saves itself as you type.")
        self.idea_edit.textChanged.connect(self._idea_changed)
        hero_layout.addWidget(self.idea_edit)
        hint = QLabel(
            "Then break it into scenes. Each scene gets one picture and "
            "one spoken line."
        )
        hint.setProperty("role", "muted")
        hint.setWordWrap(True)
        hero_layout.addWidget(hint)
        layout.addWidget(hero)

        bento = QGridLayout()
        bento.setSpacing(16)
        self.scenes_tile = TileButton("Scenes")
        self.scenes_tile.setToolTip("Open the Scenes window.")
        self.scenes_tile.clicked.connect(
            lambda: self.open_window("scenes"))
        bento.addWidget(self.scenes_tile, 0, 0)
        self.stills_tile = TileButton("Pictures")
        self.stills_tile.setToolTip("Open the Stills window.")
        self.stills_tile.clicked.connect(
            lambda: self.open_window("stills"))
        bento.addWidget(self.stills_tile, 0, 1)
        self.voice_tile = TileButton("Spoken lines")
        self.voice_tile.setToolTip("Open the Voice window.")
        self.voice_tile.clicked.connect(
            lambda: self.open_window("voice"))
        bento.addWidget(self.voice_tile, 0, 2)
        self.film_tile = TileButton("Film")
        self.film_tile.setToolTip("Open the Export window.")
        self.film_tile.clicked.connect(
            lambda: self.open_window("export"))
        bento.addWidget(self.film_tile, 0, 3)
        layout.addLayout(bento)

        self._build_checklist(layout)

        buttons = QHBoxLayout()
        self.scenes_button = DipButton("Break into scenes")
        self.scenes_button.setProperty("role", "primary")
        self.scenes_button.setToolTip(
            "Open the Scenes window. If an idea is typed and a model "
            "is picked, the breakdown starts straight away.")
        self.scenes_button.clicked.connect(self._break_from_home)
        buttons.addWidget(self.scenes_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.machine_label = QLabel()
        self.machine_label.setProperty("role", "muted")
        layout.addStretch(1)

        # One footer row: the working state on the left, the machine
        # on the right. Two rows here cost a row the tiles need.
        status_row = QHBoxLayout()
        self.lamp = StatusLamp()
        self.status_label = QLabel("Ready.")
        self.status_label.setProperty("role", "muted")
        self.status_label.setWordWrap(True)
        status_row.addWidget(self.lamp)
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.machine_label)
        layout.addLayout(status_row)
        self.setCentralWidget(central)

        QWidget.setTabOrder(self.idea_edit, self.scenes_tile)
        QWidget.setTabOrder(self.scenes_tile, self.stills_tile)
        QWidget.setTabOrder(self.stills_tile, self.voice_tile)
        QWidget.setTabOrder(self.voice_tile, self.film_tile)
        QWidget.setTabOrder(self.film_tile, self.scenes_button)

    def _build_checklist(self, layout):
        card = QFrame()
        card.setProperty("role", "card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 12, 14, 12)
        head = QHBoxLayout()
        self.checklist_title = QLabel("First steps")
        self.checklist_title.setProperty("role", "section")
        head.addWidget(self.checklist_title, 1)
        hide_button = DipButton("Hide this list")
        hide_button.setProperty("role", "small")
        hide_button.setToolTip(
            "Hide the first-steps list. Bring it back from Help.")
        hide_button.clicked.connect(self._hide_checklist)
        head.addWidget(hide_button)
        card_layout.addLayout(head)
        self._checklist_rows = {}
        steps = (
            ("engine", "Open Stills", "stills"),
            ("voice", "Open Voice", "voice"),
            ("ffmpeg", "Open Export", "export"),
            ("ollama", "Open Scenes", "scenes"),
        )
        for key, button_text, window_key in steps:
            row = QHBoxLayout()
            mark = QLabel("...")
            mark.setFixedWidth(18)
            row.addWidget(mark)
            text = QLabel()
            text.setProperty("role", "muted")
            text.setWordWrap(True)
            row.addWidget(text, 1)
            if button_text is not None:
                button = DipButton(button_text)
                button.setProperty("role", "small")
                button.clicked.connect(
                    lambda _c, k=window_key: self.open_window(k))
                row.addWidget(button)
            card_layout.addLayout(row)
            self._checklist_rows[key] = (mark, text)
        self.checklist_card = card
        layout.addWidget(card)

    def _set_checklist_row(self, key, done, text):
        mark, label = self._checklist_rows[key]
        if done is None:
            mark.setText("...")
            mark.setProperty("role", "muted")
        elif done:
            mark.setText("\u2713")
            mark.setProperty("role", "ok")
        else:
            mark.setText("\u25cb")
            mark.setProperty("role", "muted")
        mark.style().unpolish(mark)
        mark.style().polish(mark)
        label.setText(text)

    def _refresh_checklist(self):
        if self._checklist_dismissed:
            self.checklist_card.setVisible(False)
            return
        self.checklist_card.setVisible(True)
        engine = stills.preferred_engine()
        voice_ready, _s = voice.is_ready(self.state.project.voice)
        ffmpeg_ok = video.find_ffmpeg() is not None
        ollama_ok = self._ollama_ok
        self._set_checklist_row(
            "engine", engine is not None,
            "Picture engine ready." if engine is not None
            else "Set up a picture engine.")
        self._set_checklist_row(
            "voice", voice_ready,
            "Voice ready." if voice_ready else "Set up the voice.")
        self._set_checklist_row(
            "ffmpeg", ffmpeg_ok,
            "ffmpeg is ready." if ffmpeg_ok
            else "ffmpeg is missing. Open Export to install it.")
        if ollama_ok is None:
            self._set_checklist_row("ollama", None, "Checking Ollama.")
        else:
            self._set_checklist_row(
                "ollama", ollama_ok,
                "Ollama is running." if ollama_ok
                else "Ollama is not running. "
                "Scenes can be typed by hand.")
        done = sum(1 for ok in (engine is not None, voice_ready,
                                ffmpeg_ok, ollama_ok is True) if ok)
        if done == 4:
            self.checklist_title.setText(
                "All set up. Hide this list when you are done.")
        else:
            self.checklist_title.setText(
                f"First steps: {done} of 4 done.")

    def _hide_checklist(self):
        self._checklist_dismissed = True
        settings().setValue("checklist/dismissed", 1)
        self.checklist_card.setVisible(False)

    def _show_checklist(self):
        self._checklist_dismissed = False
        settings().setValue("checklist/dismissed", 0)
        self._refresh_checklist()

    def _check_ollama(self):
        worker = Worker(lambda report, cancel: _ollama_reachable())
        self._ollama_worker = worker
        worker.done.connect(_weak_slot(self, "_on_ollama_checked"))
        worker.error.connect(_weak_slot(self, "_on_ollama_error"))
        worker.start()

    def _on_ollama_checked(self, reachable):
        self._ollama_worker = None
        self._ollama_ok = bool(reachable)
        self._refresh_checklist()

    def _on_ollama_error(self, _exc):
        self._ollama_worker = None
        self._ollama_ok = False
        self._refresh_checklist()

    def _break_from_home(self):
        """The home button does what it says: open the Scenes window
        and start the breakdown when an idea is typed and a model is
        picked. Otherwise the window just opens, ready for the user
        to finish setting up."""
        window = self.open_window("scenes")
        window.start_breakdown_if_ready()

    def open_window(self, key):
        builders = {
            "scenes": ScenesWindow,
            "stills": StillsWindow,
            "voice": VoiceWindow,
            "export": ExportWindow,
            "howto": HowToWindow,
            "debug": DebugReportWindow,
        }
        window = self._windows.get(key)
        if window is None:
            window = builders[key](self.state)
            owner = weakref.ref(self)

            def _forget(_obj, k=key, ref=owner):
                main = ref()
                if main is not None:
                    main._windows.pop(k, None)

            window.destroyed.connect(_forget)
            self._windows[key] = window
        window.show()
        window.raise_()
        window.activateWindow()
        return window

    def _refresh(self):
        self.project_label.setText(
            f"Film: {self.state.project_name()}")
        dates_text = film_dates_text(self.state.project)
        self.project_dates_label.setText(dates_text)
        self.project_dates_label.setVisible(bool(dates_text))
        idea = self.state.project.idea
        project_dir = self.state.project_dir
        if project_dir != self._idea_project_dir:
            # A different film is on the bench: stop any pending save
            # for the old film and show the new film's idea.
            self._idea_timer.stop()
            self._idea_project_dir = project_dir
            self._saved_idea = idea
            if self.idea_edit.toPlainText() != idea:
                self.idea_edit.blockSignals(True)
                self.idea_edit.setPlainText(idea)
                self.idea_edit.blockSignals(False)
        elif idea != self._saved_idea:
            # Same film, but the idea changed elsewhere: show it.
            self._saved_idea = idea
            if self.idea_edit.toPlainText() != idea:
                self.idea_edit.blockSignals(True)
                self.idea_edit.setPlainText(idea)
                self.idea_edit.blockSignals(False)
        project = self.state.project
        project_dir = self.state.project_dir
        count = len(project.scenes)
        stills_ready = voice_ready = clips_ready = 0
        voice_id = project.voice
        if project_dir is not None:
            for index, scene in enumerate(project.scenes):
                # Out of date counts as to make, not ready: the same
                # staleness checks the Stills, Voice, and Export
                # windows use, so every window agrees.
                if not scenes_mod.still_is_stale(scene, project_dir):
                    stills_ready += 1
                if not scenes_mod.voice_is_stale(scene, project_dir,
                                                 voice_id):
                    voice_ready += 1
                motion = video.motion_for_scene(index, scene.motion)
                if not scenes_mod.clip_is_stale(scene, project_dir,
                                                voice_id, motion):
                    clips_ready += 1
        film = (project_dir / scenes_mod.FILM_FILE
                if project_dir is not None else None)
        film_built = film is not None and film.exists()
        self.scenes_tile.set_fact(
            count, "Tap to open" if count else "Break the idea down")
        if count:
            self.stills_tile.set_fact(
                f"{stills_ready}/{count}", "Tap to open Stills")
            self.voice_tile.set_fact(
                f"{voice_ready}/{count}", "Tap to open Voice")
        else:
            self.stills_tile.set_fact("-", "No scenes yet")
            self.voice_tile.set_fact("-", "No scenes yet")
        self.film_tile.set_fact(
            "Built" if film_built else "Not yet",
            "Tap to open Export" if film_built else "Not built yet")
        self._refresh_machine()
        self._refresh_checklist()
        self._refresh_recent_menu()

    def _refresh_machine(self):
        """The machine strip: free memory and free disk, right now."""
        self.machine_label.setText(
            f"Free memory: about "
            f"{_gb_text(machine.mem_available_mb())}. Free disk: about "
            f"{_gb_text(machine.disk_free_mb(str(Path.home())))}.")

    def _refresh_recent_menu(self):
        self._recent_menu.clear()
        recent = _load_recent()
        # Drop entries whose folder is gone, and save the pruned
        # list, so the menu only ever offers films that open.
        kept = [path for path in recent if Path(path).is_dir()]
        if len(kept) != len(recent):
            settings().setValue("recent", kept)
            recent = kept
        if not recent:
            action = QAction("No recent films", self)
            action.setEnabled(False)
            self._recent_menu.addAction(action)
            return
        for path in recent:
            action = QAction(_recent_label(path), self)
            action.setToolTip(path)
            action.triggered.connect(
                lambda _c, p=path: self._open_recent(p))
            self._recent_menu.addAction(action)

    def _remember_recent(self, project_dir):
        recent = _load_recent()
        text = str(project_dir)
        if text in recent:
            recent.remove(text)
        recent.insert(0, text)
        # Always store a real list, even with one item.
        settings().setValue("recent", recent[:MAX_RECENT])
        self._refresh_recent_menu()

    def _idea_changed(self):
        self._idea_timer.start()

    def _save_idea(self):
        # Capture the text first: creating the project fires a refresh
        # that clears the box, so reading it afterwards loses the idea.
        text = self.idea_edit.toPlainText()
        if self.state.project_dir is None:
            self.state.new_project("untitled film")
        self.state.project.idea = text
        self.state.save()
        self._saved_idea = text
        self._idea_project_dir = self.state.project_dir
        if self.idea_edit.toPlainText() != text:
            self.idea_edit.blockSignals(True)
            self.idea_edit.setPlainText(text)
            self.idea_edit.blockSignals(False)

    def _flush_idea(self):
        # Save pending typing before switching films, so it lands in
        # the film it was typed for and not the next one.
        if self.state.project_dir is not None:
            self._idea_timer.stop()
            self._save_idea()

    def _new_project(self):
        name, ok = QInputDialog.getText(
            self, "New film", "Name the film:")
        if not ok or not name.strip():
            return
        self._flush_idea()
        target = self.state.new_project(name.strip())
        self._remember_recent(target)
        self.status_label.setText(f"New film: {target.name}.")

    def _open_project(self):
        start = str(paths.projects_dir())
        chosen = QFileDialog.getExistingDirectory(
            self, "Open a film folder", start)
        if not chosen:
            return
        self._open_recent(chosen)

    def _open_recent(self, path):
        self._flush_idea()
        try:
            target = self.state.open_project(path)
        except HicksFieldError as exc:
            self.lamp.set_state("error")
            self.status_label.setText(exc.plain_text())
            return
        self._remember_recent(target)
        self.lamp.set_state("done")
        self.status_label.setText(f"Opened {target.name}.")

    def _save_project(self):
        if self.state.project_dir is None:
            self._new_project()
            return
        self.state.save()
        self.lamp.set_state("done")
        self.status_label.setText("Saved.")

    def _on_job_changed(self, owner):
        if owner is None:
            self._machine_timer.stop()
            self.lamp.set_state("idle")
            self.status_label.setText("Ready.")
        else:
            self.lamp.set_state("working")
            self.status_label.setText(f"Working: {owner}.")
            # A job is running: refresh the machine strip now, and
            # keep it fresh every few seconds until the job ends.
            self._refresh_machine()
            self._machine_timer.start()

    def _about(self):
        QMessageBox.about(
            self, "About HicksField",
            "HicksField 0.4.0\n\nA free local film studio by "
            "Broke n' Bored Studio.\n\nYour idea in, a short film out. "
            "Everything runs on this computer.")

    def closeEvent(self, event):
        settings().setValue("geometry/main", self.saveGeometry())
        if self.state.job_lock.busy:
            answer = QMessageBox.question(
                self, "A job is running",
                f"{self.state.job_owner} is still running. Close anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        for window in list(self._windows.values()):
            window.close()
        super().closeEvent(event)


def _placeholder_icon():
    pixmap = QPixmap(96, 96)
    pixmap.fill(PANEL_LIGHT)
    painter = QPainter(pixmap)
    try:
        painter.setPen(QPen(QColor(BRASS_DARK), 2))
        painter.drawRect(6, 6, 84, 84)
        painter.drawLine(6, 30, 90, 30)
        painter.drawLine(6, 66, 90, 66)
    finally:
        painter.end()
    return QIcon(pixmap)


class ScenesWindow(ModeWindow):
    def __init__(self, state):
        super().__init__(state, "Scenes", "scenes", 900, 700)
        # This window holds the most rows of any window: tighter
        # spacing and margins are what leave room for three
        # film-frame rows in the list below.
        self.layout.setSpacing(6)
        self.layout.setContentsMargins(12, 10, 12, 10)
        self._worker = None
        self._ollama_ok = False
        self._model_infos = {}
        self._breakdown_running = False
        self._incoming = []
        # How many arrived scenes the list already shows, and whether
        # the list is showing the breakdown at all: _show_incoming
        # appends only the new rows instead of rebuilding the list
        # per arrival.
        self._incoming_shown = 0
        self._incoming_list_ready = False
        # True while the model check runs: Refresh stays disabled
        # until it lands, so a double-click cannot start two checks.
        self._checking_models = False
        # The row whose words are in the edit boxes, or -1 when the
        # boxes hold nothing to save: autosave writes the boxes back
        # to this scene before another scene is loaded.
        self._loaded_row = -1
        # Guards _load_scene against its own refresh: saving the
        # boxes fires project_changed, which rebuilds the list and
        # would otherwise load a scene twice.
        self._loading_scene = False
        # Guards _refresh against itself: clearing the list loads a
        # scene, which autosaves and announces the change, which
        # refreshes again. Clearing the list while it is being
        # cleared crashes Qt, so a nested refresh is deferred.
        self._refreshing = False
        self._refresh_pending = False
        # Snapshots of the scene list for Undo, newest last.
        self._undo_stack = []
        # The project the window last refreshed for. A new project
        # object means the film changed: the undo stack belongs to
        # the old film and is cleared, and the edit boxes are
        # autosaved into the old film before the reload.
        self._seen_project = state.project
        self._seen_project_dir = state.project_dir
        self.idea_echo = QLabel()
        self.idea_echo.setProperty("role", "muted")
        self.idea_echo.setWordWrap(True)
        self.layout.addWidget(self.idea_echo)

        row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setPlaceholderText("Pick a model")
        self.model_combo.setToolTip(
            "Pick the model that breaks your idea into scenes.")
        self.model_combo.currentIndexChanged.connect(self._on_model_picked)
        row.addWidget(self.model_combo, 1)
        self.length_combo = QComboBox()
        self.length_combo.setToolTip(
            "How many scenes the breakdown makes. Short is quickest "
            "to build, Long takes longest.")
        self.length_combo.addItem("Short (3 to 5 scenes, quickest)",
                                  "short")
        self.length_combo.addItem("Medium (3 to 8 scenes, the usual)",
                                  "medium")
        self.length_combo.addItem("Long (8 to 12 scenes, takes longest)",
                                  "long")
        self.length_combo.setCurrentIndex(1)
        row.addWidget(self.length_combo)
        self.refresh_button = DipButton("Refresh models")
        self.refresh_button.setToolTip(
            "Look for Ollama and its models again.")
        self.refresh_button.clicked.connect(self._refresh_models)
        row.addWidget(self.refresh_button)
        break_button = DipButton("Break into scenes")
        break_button.setProperty("role", "primary")
        break_button.setToolTip(
            "Ask the model to break the idea into scenes.")
        break_button.setEnabled(False)
        break_button.clicked.connect(self._break_into_scenes)
        row.addWidget(break_button)
        self.break_button = break_button
        self.layout.addLayout(row)

        self.model_note = QLabel(
            "Pick the model that breaks your idea into scenes.")
        self.model_note.setProperty("role", "muted")
        self.model_note.setWordWrap(True)
        self.layout.addWidget(self.model_note)

        self.ollama_label = QLabel()
        self.ollama_label.setProperty("role", "muted")
        self.ollama_label.setWordWrap(True)
        self.layout.addWidget(self.ollama_label)

        self.breakdown_progress = QProgressBar()
        self.breakdown_progress.setVisible(False)
        self.layout.addWidget(self.breakdown_progress)
        breakdown_row = QHBoxLayout()
        self.breakdown_cancel = DipButton("Cancel the breakdown")
        self.breakdown_cancel.setVisible(False)
        self.breakdown_cancel.clicked.connect(self._cancel_breakdown)
        breakdown_row.addWidget(self.breakdown_cancel)
        breakdown_row.addStretch(1)
        self.layout.addLayout(breakdown_row)

        list_label = QLabel("Scenes:")
        list_label.setProperty("role", "section")
        self.layout.addWidget(list_label)
        self.scene_list = QListWidget()
        self.scene_list.setDragDropMode(
            QAbstractItemView.InternalMove)
        # Three whole film-frame rows: 3 * 64 + 2 frame. Without a
        # minimum the list is the one the layout squeezes, and a
        # single row is all that shows.
        self.scene_list.setMinimumHeight(194)
        self.scene_list.setToolTip(
            "Drag a scene to move it, or use Move up and Move down.")
        self.scene_list.currentRowChanged.connect(self._load_scene)
        self.scene_list.model().rowsMoved.connect(self._on_rows_moved)
        self.layout.addWidget(self.scene_list, 1)

        self.edit_label = QLabel("The chosen scene:")
        self.edit_label.setProperty("role", "section")
        self.layout.addWidget(self.edit_label)
        prompt_label = QLabel("Picture prompt:")
        self.layout.addWidget(prompt_label)
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setPlaceholderText(
            "What the picture shows.")
        self.prompt_edit.setMaximumHeight(70)
        self.prompt_edit.setToolTip(
            "What the picture shows. One still picture per scene.")
        self.layout.addWidget(self.prompt_edit)
        narration_label = QLabel("Spoken line:")
        self.layout.addWidget(narration_label)
        self.narration_edit = QTextEdit()
        self.narration_edit.setPlaceholderText(
            "What the voice says over this picture.")
        self.narration_edit.setMaximumHeight(70)
        self.narration_edit.setToolTip(
            "What the voice says over this picture.")
        self.layout.addWidget(self.narration_edit)
        motion_row = QHBoxLayout()
        motion_label = QLabel("Camera move:")
        motion_row.addWidget(motion_label)
        self.motion_combo = QComboBox()
        self.motion_combo.setToolTip(
            "How the camera moves over this scene's picture. Auto "
            "lets the film pick a move.")
        self.motion_combo.addItem("Auto (the film picks)", "")
        self.motion_combo.addItem("Push in", "zoomin")
        self.motion_combo.addItem("Pull out", "zoomout")
        self.motion_combo.addItem("Pan left", "panleft")
        self.motion_combo.addItem("Pan right", "panright")
        self.motion_combo.addItem("Still (no move)", "still")
        motion_row.addWidget(self.motion_combo, 1)
        motion_row.addStretch(1)
        self.layout.addLayout(motion_row)

        edit_row = QHBoxLayout()
        self.save_button = DipButton("Save this scene")
        self.save_button.clicked.connect(self._save_scene)
        edit_row.addWidget(self.save_button)
        self.undo_button = DipButton("Undo")
        self.undo_button.setToolTip(
            "Undo the last change to the scenes.")
        self.undo_button.setEnabled(False)
        self.undo_button.clicked.connect(self._undo)
        edit_row.addWidget(self.undo_button)
        self._undo_shortcut = QShortcut(QKeySequence("Ctrl+Z"), self)
        self._undo_shortcut.activated.connect(self._undo)
        self.add_button = DipButton("Add a scene")
        self.add_button.setToolTip("Add an empty scene at the end.")
        self.add_button.clicked.connect(self._add_scene)
        edit_row.addWidget(self.add_button)
        self.remove_button = DipButton("Remove this scene")
        self.remove_button.setToolTip(
            "Remove the chosen scene from the film.")
        self.remove_button.clicked.connect(self._remove_scene)
        edit_row.addWidget(self.remove_button)
        self.move_up_button = DipButton("Move up")
        self.move_up_button.setToolTip("Move the chosen scene up.")
        self.move_up_button.clicked.connect(
            lambda: self._move_scene(-1))
        edit_row.addWidget(self.move_up_button)
        self.move_down_button = DipButton("Move down")
        self.move_down_button.setToolTip("Move the chosen scene down.")
        self.move_down_button.clicked.connect(
            lambda: self._move_scene(1))
        edit_row.addWidget(self.move_down_button)
        edit_row.addStretch(1)
        self.layout.addLayout(edit_row)
        self.finish_layout()
        state.project_changed.connect(_weak_slot(self, "_refresh"))
        self._refresh()
        self._load_scene(-1)
        self._refresh_models()

        QWidget.setTabOrder(self.model_combo, self.length_combo)
        QWidget.setTabOrder(self.length_combo, self.refresh_button)
        QWidget.setTabOrder(self.refresh_button, self.break_button)
        QWidget.setTabOrder(self.break_button, self.scene_list)
        QWidget.setTabOrder(self.scene_list, self.prompt_edit)
        QWidget.setTabOrder(self.prompt_edit, self.narration_edit)
        QWidget.setTabOrder(self.narration_edit, self.motion_combo)
        QWidget.setTabOrder(self.motion_combo, self.save_button)
        QWidget.setTabOrder(self.save_button, self.add_button)
        QWidget.setTabOrder(self.add_button, self.remove_button)
        QWidget.setTabOrder(self.remove_button, self.move_up_button)
        QWidget.setTabOrder(self.move_up_button, self.move_down_button)

    def _refresh(self):
        if self._refreshing:
            # Already refreshing: run once more when the outer
            # refresh finishes, instead of clearing the list twice.
            self._refresh_pending = True
            return
        self._refreshing = True
        try:
            while True:
                self._refresh_pending = False
                self._refresh_now()
                if not self._refresh_pending:
                    break
        finally:
            self._refreshing = False

    def _refresh_now(self):
        if (self.state.project is not self._seen_project
                or self.state.project_dir != self._seen_project_dir):
            # The film changed since the last refresh: save the
            # boxes into the OLD film first (the reload below would
            # otherwise throw the words away), then drop the old
            # film's undo stack. The boxes belong to no row of the
            # new film yet, so forget the loaded row before the list
            # is cleared: clearing loads row -1, which must not
            # autosave the old words into a new scene.
            self._autosave_previous_project()
            self._undo_stack.clear()
            self._seen_project = self.state.project
            self._seen_project_dir = self.state.project_dir
            self._loaded_row = -1
        bar = self.scene_list.verticalScrollBar()
        scroll = bar.value()
        row = self.scene_list.currentRow()
        idea = self.state.project.idea
        self.idea_echo.setText(
            f"Idea: {idea}" if idea else
            "No idea typed yet. Type one on the main "
            "window, or add scenes by hand here.")
        if self._breakdown_running:
            self._show_incoming()
        else:
            self.scene_list.clear()
            for index, scene in enumerate(self.state.project.scenes):
                self._add_scene_item(index, scene)
            if not self.state.project.scenes:
                self._add_empty_hint_item()
        if (self.state.project.scenes
                and 0 <= row < self.scene_list.count()):
            self.scene_list.setCurrentRow(row)
        bar.setValue(scroll)
        self._refresh_undo_button()

    def _autosave_previous_project(self):
        """Write the edit boxes into the film the window last
        refreshed for, when they changed. Used on a film switch: by
        the time the refresh runs, the state already holds the new
        film, so the ordinary autosave would write the old film's
        words into the new film's scene. No undo snapshot: the
        stack belongs to the old film and is cleared with it."""
        old = self._seen_project
        row = self._loaded_row
        if old is None or not (0 <= row < len(old.scenes)):
            return
        scene = old.scenes[row]
        prompt = self.prompt_edit.toPlainText().strip()
        narration = self.narration_edit.toPlainText().strip()
        motion = self.motion_combo.currentData() or ""
        if (prompt == scene.prompt and narration == scene.narration
                and motion == scene.motion):
            return
        scene.prompt = prompt
        scene.narration = narration
        scene.motion = motion
        if self._seen_project_dir is not None:
            scenes_mod.save_project(old, self._seen_project_dir)

    def _refresh_undo_button(self):
        """Undo is offered only when it can act: a snapshot on the
        stack, and no breakdown running."""
        self.undo_button.setEnabled(
            bool(self._undo_stack) and not self._breakdown_running)

    def _add_empty_hint_item(self):
        """A hint inside an empty list, so the empty box says what
        to do next instead of sitting black and blank."""
        frame = QFrame()
        frame.setProperty("role", "scene")
        frame.setAttribute(Qt.WA_TransparentForMouseEvents)
        hint_layout = QHBoxLayout(frame)
        hint_layout.setContentsMargins(8, 6, 8, 6)
        hint = QLabel(
            "No scenes yet. Break the idea down above, "
            "or add one below.")
        hint.setProperty("role", "muted")
        hint.setWordWrap(True)
        hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        hint_layout.addWidget(hint, 1)
        item = QListWidgetItem()
        item.setFlags(Qt.NoItemFlags)
        item.setSizeHint(QSize(0, 64))
        self.scene_list.addItem(item)
        self.scene_list.setItemWidget(item, frame)

    def _add_scene_item(self, index, scene, row=None):
        """One scene as a film-frame card: thumbnail, prompt, and the
        state marks for picture and voice. Both ready earns the green
        edge. Ready means fresh, not just present: an out-of-date
        picture or line counts as to make. `row` inserts at a
        position (a breakdown scene ahead of the waiting rows);
        without it the card goes at the end."""
        project_dir = self.state.project_dir
        still_exists = bool(
            scene.still and project_dir is not None
            and (project_dir / scene.still).exists())
        audio_exists = bool(
            scene.audio and project_dir is not None
            and (project_dir / scene.audio).exists())
        # The same staleness checks the Stills and Voice windows
        # use, so every window agrees. A missing file is stale, so
        # the existence check also guards the None project dir.
        still_ok = still_exists and not scenes_mod.still_is_stale(
            scene, project_dir)
        audio_ok = audio_exists and not scenes_mod.voice_is_stale(
            scene, project_dir, self.state.project.voice)
        frame = QFrame()
        frame.setProperty(
            "role", "scene-done" if still_ok and audio_ok else "scene")
        frame.setFixedHeight(64)
        # The row is display only: clicks pass through to the list, so
        # the row still selects and drags like a plain item.
        frame.setAttribute(Qt.WA_TransparentForMouseEvents)
        row_layout = QHBoxLayout(frame)
        row_layout.setContentsMargins(8, 6, 8, 6)
        row_layout.setSpacing(10)
        thumb = QLabel()
        thumb.setFixedSize(48, 48)
        thumb.setAttribute(Qt.WA_TransparentForMouseEvents)
        if still_exists:
            pixmap = QPixmap(str(project_dir / scene.still))
            thumb.setPixmap(pixmap.scaled(
                48, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            thumb.setPixmap(_placeholder_icon().pixmap(48, 48))
        row_layout.addWidget(thumb)
        text_box = QVBoxLayout()
        text_box.setSpacing(2)
        prompt = scene.prompt or "(no picture prompt yet)"
        title = QLabel(f"Scene {index + 1}: {prompt[:70]}")
        title.setAttribute(Qt.WA_TransparentForMouseEvents)
        text_box.addWidget(title)
        motion_text = {
            "": "Auto move",
            "zoomin": "Push in",
            "zoomout": "Pull out",
            "panleft": "Pan left",
            "panright": "Pan right",
            "still": "Still",
        }.get(scene.motion, scene.motion or "Auto move")
        if still_ok:
            still_mark = "Picture ready"
        elif still_exists:
            still_mark = "Picture out of date"
        else:
            still_mark = "No picture yet"
        if audio_ok:
            audio_mark = "Voice ready"
        elif audio_exists:
            audio_mark = "Voice out of date"
        else:
            audio_mark = "No voice yet"
        marks = (still_mark, audio_mark, motion_text)
        mark_label = QLabel("  \u00b7  ".join(marks))
        mark_label.setProperty("role", "muted")
        mark_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        text_box.addWidget(mark_label)
        row_layout.addLayout(text_box, 1)
        item = QListWidgetItem()
        item.setData(Qt.UserRole, index)
        item.setSizeHint(QSize(0, 64))
        if row is None:
            self.scene_list.addItem(item)
        else:
            self.scene_list.insertItem(row, item)
        self.scene_list.setItemWidget(item, frame)

    def _add_skeleton_item(self):
        """A waiting row: the breakdown is still coming, so the list
        shows where the next scene will land instead of a gap."""
        frame = QFrame()
        frame.setProperty("role", "scene")
        frame.setFixedHeight(64)
        frame.setAttribute(Qt.WA_TransparentForMouseEvents)
        row_layout = QHBoxLayout(frame)
        row_layout.setContentsMargins(8, 6, 8, 6)
        row_layout.setSpacing(10)
        thumb = QLabel()
        thumb.setFixedSize(48, 48)
        thumb.setAttribute(Qt.WA_TransparentForMouseEvents)
        thumb.setPixmap(_placeholder_icon().pixmap(48, 48))
        row_layout.addWidget(thumb)
        title = QLabel("Scene ... still coming.")
        title.setProperty("role", "muted")
        title.setAttribute(Qt.WA_TransparentForMouseEvents)
        row_layout.addWidget(title, 1)
        item = QListWidgetItem()
        item.setFlags(Qt.NoItemFlags)
        item.setSizeHint(QSize(0, 64))
        self.scene_list.addItem(item)
        self.scene_list.setItemWidget(item, frame)

    def _show_incoming(self):
        """Show the scenes that have arrived so far, appending only
        the new rows: rebuilding the whole list per arrival flickers
        on a slow PC with a dozen scenes. The first call for a
        breakdown builds the list (scenes so far, then the waiting
        rows); later calls insert each new scene ahead of them."""
        if not self._incoming_list_ready:
            self.scene_list.clear()
            for index, scene in enumerate(self._incoming):
                self._add_scene_item(index, scene)
            for _ in range(3):
                self._add_skeleton_item()
            self._incoming_shown = len(self._incoming)
            self._incoming_list_ready = True
            return
        while self._incoming_shown < len(self._incoming):
            self._add_scene_item(
                self._incoming_shown,
                self._incoming[self._incoming_shown],
                row=self._incoming_shown)
            self._incoming_shown += 1

    def _autosave_scene(self):
        """Write the edit boxes into the loaded scene, if they
        changed. Returns True when the scene changed. An undo
        snapshot is pushed before the change, so an autosave can be
        undone like any other edit. The caller saves and refreshes."""
        row = self._loaded_row
        scenes = self.state.project.scenes
        if not (0 <= row < len(scenes)):
            return False
        scene = scenes[row]
        prompt = self.prompt_edit.toPlainText().strip()
        narration = self.narration_edit.toPlainText().strip()
        motion = self.motion_combo.currentData() or ""
        if (prompt == scene.prompt and narration == scene.narration
                and motion == scene.motion):
            return False
        self._push_undo()
        scene.prompt = prompt
        scene.narration = narration
        scene.motion = motion
        return True

    def _push_undo(self):
        """Snapshot the scene list for Undo, newest last, cap 20."""
        self._undo_stack.append(
            copy.deepcopy(self.state.project.scenes))
        if len(self._undo_stack) > 20:
            del self._undo_stack[:-20]
        self._refresh_undo_button()

    def _undo(self):
        if not self._undo_stack:
            return
        scenes = self._undo_stack.pop()
        # The boxes belong to the list being replaced, not the
        # restored one: forget them so no autosave writes the old
        # words into a restored scene.
        self._loaded_row = -1
        self.state.project.scenes = scenes
        self.state.save()
        self.state.project_changed.emit()
        self._refresh_undo_button()
        self._load_scene(self.scene_list.currentRow())
        self.show_status("done", "Undid the last change.")

    def closeEvent(self, event):
        # Do not lose the words in the boxes when the window closes.
        if self._autosave_scene():
            self.state.save()
        super().closeEvent(event)

    def _load_scene(self, row):
        if self._breakdown_running:
            return
        if self._loading_scene:
            return
        self._loading_scene = True
        try:
            # Save the loaded scene's edits before the boxes are
            # overwritten with another scene's words.
            if row != self._loaded_row and self._autosave_scene():
                self.state.save()
                self.state.project_changed.emit()
            scenes = self.state.project.scenes
            if 0 <= row < len(scenes):
                self.prompt_edit.setPlainText(scenes[row].prompt)
                self.narration_edit.setPlainText(scenes[row].narration)
                self._set_motion_combo(scenes[row].motion)
                self._loaded_row = row
            else:
                self.prompt_edit.clear()
                self.narration_edit.clear()
                self._set_motion_combo("")
                self._loaded_row = -1
            chosen = 0 <= row < len(scenes)
            self.remove_button.setEnabled(chosen)
            self.motion_combo.setEnabled(chosen)
            self.move_up_button.setEnabled(chosen and row > 0)
            self.move_down_button.setEnabled(
                chosen and row < len(scenes) - 1)
            # The header carries the guidance when nothing is chosen, so
            # no separate hint row is needed.
            self.edit_label.setText(
                "The chosen scene:" if chosen else
                "The chosen scene: pick a scene above to edit it, "
                "or add one below.")
        finally:
            self._loading_scene = False

    def _set_motion_combo(self, motion):
        """Show a scene's camera move in the picker. An unknown move
        shows Auto: the film picks, as it always did."""
        index = self.motion_combo.findData(motion)
        self.motion_combo.setCurrentIndex(index if index >= 0 else 0)

    def _scene_length(self):
        """The breakdown length the picker holds, or the usual one."""
        data = self.length_combo.currentData()
        if data in ollama.SCENE_LENGTHS:
            return data
        return ollama.DEFAULT_SCENE_LENGTH

    def _move_scene(self, step):
        row = self.scene_list.currentRow()
        scenes = self.state.project.scenes
        target = row + step
        if not (0 <= row < len(scenes)):
            return
        if not (0 <= target < len(scenes)):
            return
        # Save the boxes into the scene first, while its row is
        # still right; the edited scene then lives at the target.
        self._autosave_scene()
        self._push_undo()
        scenes[row], scenes[target] = scenes[target], scenes[row]
        self._loaded_row = target
        self.state.save()
        self.state.project_changed.emit()
        self.scene_list.setCurrentRow(target)
        self.show_status("done", "Moved the scene.")

    def _on_rows_moved(self, _parent, _start, _end, _dest, _row):
        # The drop finishes first; the order is synced on the next
        # turn of the loop, when the list has settled.
        QTimer.singleShot(0, self._sync_order_from_list)

    def _sync_order_from_list(self):
        if self._breakdown_running:
            return
        scenes = self.state.project.scenes
        order = []
        for row in range(self.scene_list.count()):
            item = self.scene_list.item(row)
            if item is None:
                return
            index = item.data(Qt.UserRole)
            if index is None or not 0 <= index < len(scenes):
                return
            order.append(index)
        if sorted(order) != list(range(len(scenes))):
            return
        if order == list(range(len(scenes))):
            return
        # Save the boxes first, while the loaded row is still right;
        # after the reorder the boxes belong to no row in particular.
        self._autosave_scene()
        self._loaded_row = -1
        self._push_undo()
        self.state.project.scenes = [scenes[i] for i in order]
        self.state.save()
        self.state.project_changed.emit()
        self.show_status("done", "Scenes reordered.")

    def _save_scene(self):
        row = self.scene_list.currentRow()
        scenes = self.state.project.scenes
        if not (0 <= row < len(scenes)):
            self.show_status("idle", "Pick a scene first.")
            return
        scene = scenes[row]
        prompt = self.prompt_edit.toPlainText().strip()
        narration = self.narration_edit.toPlainText().strip()
        motion = self.motion_combo.currentData() or ""
        if (prompt != scene.prompt or narration != scene.narration
                or motion != scene.motion):
            self._push_undo()
            scene.prompt = prompt
            scene.narration = narration
            scene.motion = motion
        self.state.save()
        self.state.project_changed.emit()
        self.show_status("done", f"Scene {row + 1} saved.")

    def _add_scene(self):
        self._push_undo()
        self.state.project.scenes.append(scenes_mod.Scene())
        self.state.save()
        self.state.project_changed.emit()
        self.scene_list.setCurrentRow(len(self.state.project.scenes) - 1)
        self.prompt_edit.setFocus()
        self.show_status("idle", "New scene. Type what the picture shows.")

    def _remove_scene(self):
        row = self.scene_list.currentRow()
        scenes = self.state.project.scenes
        if not (0 <= row < len(scenes)):
            self.show_status("idle", "Pick a scene first.")
            return
        if not self.ask_yes_no(
                "Remove this scene?",
                f"Remove scene {row + 1}? Its picture and spoken line "
                "stay on disk, but the scene leaves the film."):
            return
        # The boxes belong to the scene being removed: forget them,
        # so the refresh after the remove cannot save its words into
        # the scene that takes its row.
        self._loaded_row = -1
        self._push_undo()
        del scenes[row]
        self.state.save()
        self.state.project_changed.emit()
        self.show_status("done", f"Scene {row + 1} removed.")

    def _update_break_button(self):
        """Break is ready only when a model is picked and nothing
        else is running: a button that looks ready and then scolds
        you is worse than a disabled one."""
        ready = (self._ollama_ok and not self._breakdown_running
                 and not self._checking_models
                 and bool(self.model_combo.currentText()))
        self.break_button.setEnabled(ready)

    def _refresh_models(self):
        if self._checking_models or self._breakdown_running:
            return
        self._checking_models = True
        self.refresh_button.setEnabled(False)
        self._update_break_button()
        self.show_status("working", "Checking for Ollama.")
        worker = Worker(self._check_ollama)
        self._worker = worker
        worker.progress.connect(self._on_models_progress)
        worker.done.connect(self._on_models)
        worker.error.connect(self._on_models_error)
        worker.start()

    @staticmethod
    def _check_ollama(report, _cancel):
        infos = ollama.list_model_info()
        total = len(infos)
        if not total:
            return infos
        # The capability check is one slow round trip per model, so
        # the models are asked in parallel instead of one after
        # another -- and each answer is reported, so the wait shows
        # progress instead of silence.
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(4, total)) as pool:
            futures = {
                pool.submit(ollama.model_capabilities, info["name"]):
                info for info in infos
            }
            done = 0
            for future in concurrent.futures.as_completed(futures):
                info = futures[future]
                try:
                    info["capabilities"] = future.result()
                except Exception:  # noqa: BLE001 - unknown, not incapable
                    info["capabilities"] = []
                done += 1
                report("models", done / total,
                       f"{done} of {total} models checked")
        return infos

    def _on_models_progress(self, label, fraction, detail):
        if label == "models":
            self.show_status("working",
                             f"Checking models. {detail}")

    def _on_models(self, infos):
        self._worker = None
        self._checking_models = False
        self.refresh_button.setEnabled(True)
        self._ollama_ok = True
        self._model_infos = {info["name"]: info for info in infos}
        self.model_combo.clear()
        names = [info["name"] for info in infos]
        self.model_combo.addItems(names)
        self.model_combo.setCurrentIndex(-1)
        self._update_break_button()
        if infos:
            self._show_model_recommendation()
            self.show_status(
                "done",
                f"Ollama is running with {plural(len(infos), 'model')}. "
                "Pick one, then break the idea into scenes.")
        else:
            self.show_status(
                "idle",
                "Ollama is running but has no models. Pull one first, "
                "or type scenes by hand below.")
        self.ollama_label.setText(
            "Ollama is running." if infos else
            "Ollama is running, no models found.")

    def _on_model_picked(self, _index):
        name = self.model_combo.currentText()
        if not name:
            self._show_model_recommendation()
        else:
            info = self._model_infos.get(name, {})
            self.model_note.setText(self._model_note_text(name, info))
        self._update_break_button()

    def _show_model_recommendation(self):
        infos = list(self._model_infos.values())
        best = ollama.recommended_model(infos) if infos else None
        if best is not None:
            self.model_note.setText(
                f"Recommended: {best['name']} "
                f"({format_bytes(best['size'])}). Pick a model above.")
        elif infos:
            self.model_note.setText(
                "Pick a model above. None of the installed models fits "
                "this PC well. The note below the picker says whether "
                "the picked model fits.")
        else:
            self.model_note.setText(
                "Pick the model that breaks your idea into scenes.")

    @staticmethod
    def _model_note_text(name, info):
        size = info.get("size", 0)
        size_text = format_bytes(size) if size else "size unknown"
        if not ollama.model_can_chat(info.get("capabilities", [])):
            return (f"{name} · {size_text} · this model cannot break an "
                    "idea into scenes (it does not chat). Pick another "
                    "one, or try it anyway.")
        if not ollama.model_fits(size):
            available = machine.mem_available_mb()
            return (f"{name} · {size_text} · too big for this PC "
                    f"({_gb_text(available)} of memory free). It may "
                    "run slowly or fail. You can still try it.")
        return f"{name} · {size_text} · fits this PC."

    @staticmethod
    def _model_warning(name, info):
        """Plain-words warning when a model may not do the job, or ""
        when it looks fine. Warn, never refuse: the caller asks."""
        size = info.get("size", 0)
        if not ollama.model_can_chat(info.get("capabilities", [])):
            return (f"{name} does not chat, so it probably cannot break "
                    "the idea into scenes. Try it anyway?")
        if not ollama.model_fits(size):
            available = machine.mem_available_mb()
            return (f"{name} is {format_bytes(size)} and this PC has "
                    f"{_gb_text(available)} of memory free right now. "
                    "It may run slowly or fail. Try it anyway?")
        return ""

    def _on_models_error(self, exc):
        self._worker = None
        self._checking_models = False
        self.refresh_button.setEnabled(True)
        self._ollama_ok = False
        self._model_infos = {}
        self.model_combo.clear()
        self._update_break_button()
        self.ollama_label.setText(
            "Ollama is not running. Scenes can be typed by hand below.")
        if isinstance(exc, OllamaUnreachableError):
            self.show_status("idle", exc.plain_text())
        else:
            self.show_plain_error(exc)

    def start_breakdown_if_ready(self):
        """Start the breakdown when an idea is typed and a model is
        picked; otherwise stay open for the user to finish setting
        up. Returns True when the breakdown started. The home
        button calls this so it does what it says."""
        if not self.state.project.idea.strip():
            return False
        if not self.model_combo.currentText():
            return False
        if self._breakdown_running:
            return False
        self._break_into_scenes()
        return self._breakdown_running

    def _break_into_scenes(self):
        if not self.state.project.idea.strip():
            self.show_status(
                "idle",
                "Type the idea on the main window first.")
            return
        model = self.model_combo.currentText()
        if not model:
            self.show_status(
                "idle",
                "Pick a model first, or type scenes by hand below.")
            return
        warning = self._model_warning(
            model, self._model_infos.get(model, {}))
        if warning:
            if not self.ask_yes_no("This model may not work.", warning):
                self.show_status(
                    "idle", "Left it. Pick another model.")
                return
        if self.state.project.scenes:
            if not self.ask_yes_no(
                    "Replace the current scenes?",
                    f"Replace the "
                    f"{plural(len(self.state.project.scenes), 'scene')} "
                    "with new ones from the model?"):
                self.show_status("idle", "Kept the current scenes.")
                return
        if not self.state.try_job("Breaking the idea into scenes"):
            self.show_status(
                "error",
                f"Already busy: {self.state.job_owner}. Wait for it to "
                "finish, or cancel it.")
            return
        self._breakdown_running = True
        self._incoming = []
        self._incoming_shown = 0
        self._incoming_list_ready = False
        self._set_breakdown_ui(True)
        self._refresh()
        self.show_status("working", "Breaking the idea into scenes.")
        # The length is read here, on the window's thread: the worker
        # must not touch the picker.
        length = self._scene_length()
        worker = Worker(
            lambda report, cancel: self._run_breakdown(
                model, length, report, cancel),
            on_finish=self.state.finish_job)
        self._worker = worker
        worker.progress.connect(self._on_breakdown_progress)
        worker.done.connect(self._on_breakdown)
        worker.error.connect(self._on_breakdown_error)
        worker.start()

    def _run_breakdown(self, model, length, report, cancel):
        def _on_scene(scene):
            report("scene", None, json.dumps(scene))

        return ollama.break_into_scenes_streaming(
            self.state.project.idea, model, _on_scene,
            report=report, cancel_event=cancel, scene_length=length)

    def _set_breakdown_ui(self, running):
        self.breakdown_progress.setVisible(running)
        if running:
            self.breakdown_progress.setRange(0, 0)
        self.breakdown_cancel.setVisible(running)
        if running:
            self.break_button.setEnabled(False)
        else:
            self._update_break_button()
        self.refresh_button.setEnabled(
            not running and not self._checking_models)
        self.model_combo.setEnabled(not running)
        self.length_combo.setEnabled(not running)
        self.scene_list.setEnabled(not running)
        self.prompt_edit.setEnabled(not running)
        self.narration_edit.setEnabled(not running)
        self.motion_combo.setEnabled(not running)
        self.save_button.setEnabled(not running)
        self.add_button.setEnabled(not running)
        self.remove_button.setEnabled(not running)
        self.move_up_button.setEnabled(not running)
        self.move_down_button.setEnabled(not running)
        self._refresh_undo_button()

    def _cancel_breakdown(self):
        if self._worker is not None:
            self._worker.cancel()
            self.show_status("working", "Stopping.")

    def _on_breakdown_progress(self, label, fraction, detail):
        if label == "scene":
            try:
                data = json.loads(detail)
            except ValueError:
                return
            self._incoming.append(scenes_mod.Scene(
                prompt=data.get("prompt", ""),
                narration=data.get("narration", "")))
            self._show_incoming()
            return
        if label == "loading":
            # Still waiting for the first headers: the model is
            # loading, not the server down.
            self.show_status(
                "working",
                f"Ollama is still loading the model {detail}.")
            return
        self.show_status(
            "working",
            f"Breaking the idea into scenes. {detail}")

    def _commit_incoming(self):
        self._breakdown_running = False
        self._set_breakdown_ui(False)
        # The breakdown replaces the whole scene list: the boxes
        # belong to the old list, so forget them before the swap, or
        # the refresh would save their words into a new scene.
        self._loaded_row = -1
        if self._incoming:
            # The breakdown replaces hand-typed scenes: snapshot
            # first, so the replace can be undone.
            self._push_undo()
            self.state.project.scenes = list(self._incoming)
            self.state.save()
            self.state.project_changed.emit()
        else:
            self._refresh()
        self._incoming = []
        self._load_scene(self.scene_list.currentRow())

    def _on_breakdown(self, result):
        self._worker = None
        scenes, dropped, _raw = result
        self._commit_incoming()
        note = ""
        if dropped:
            note = (f" {plural(dropped, 'line')} did not fit a scene.")
        self.show_status(
            "done",
            f"Made {plural(len(scenes), 'scene')}.{note}")

    def _on_breakdown_error(self, exc):
        self._worker = None
        if isinstance(exc, CancelledError):
            kept = len(self._incoming)
            self._commit_incoming()
            if kept:
                self.show_status(
                    "idle",
                    f"Stopped. Kept the {plural(kept, 'scene')} made "
                    "so far.")
            else:
                self.show_status(
                    "idle", "Stopped. Kept the current scenes.")
            return
        self._breakdown_running = False
        self._set_breakdown_ui(False)
        self._incoming = []
        self._refresh()
        self._load_scene(self.scene_list.currentRow())
        if isinstance(exc, OllamaUnreachableError):
            self.show_status("idle", exc.plain_text())
            self.ollama_label.setText(
                "Ollama is not running. Scenes can be typed by hand "
                "below.")
        else:
            self.show_plain_error(exc)


class _StillPreview(QLabel):
    """A picture preview that rescales when the pane resizes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap = None
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(200, 200)

    def set_pixmap(self, pixmap):
        self._pixmap = pixmap
        self._rescale()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self):
        if self._pixmap is None or self._pixmap.isNull():
            self.clear()
            return
        size = self.size()
        if size.width() <= 0 or size.height() <= 0:
            return
        self.setPixmap(self._pixmap.scaled(
            size, Qt.KeepAspectRatio, Qt.SmoothTransformation))


class _EngineCard(QFrame):
    """One engine: its name, its state, its Set up button."""

    def __init__(self, engine, on_setup, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.setProperty("role", "card")
        layout = QVBoxLayout(self)
        name_row = QHBoxLayout()
        name = QLabel(engine.label)
        name.setProperty("role", "section")
        name_row.addWidget(name)
        self.badge = QLabel("Recommended")
        self.badge.setProperty("role", "badge")
        self.badge.setVisible(False)
        name_row.addWidget(self.badge)
        name_row.addStretch(1)
        layout.addLayout(name_row)
        blurb = QLabel(engine.description)
        blurb.setProperty("role", "muted")
        blurb.setWordWrap(True)
        layout.addWidget(blurb)
        self.status = QLabel()
        self.status.setProperty("role", "muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        row = QHBoxLayout()
        self.setup_button = DipButton("Set up")
        self.setup_button.clicked.connect(on_setup)
        row.addWidget(self.setup_button)
        self.cancel_button = DipButton("Cancel")
        self.cancel_button.setVisible(False)
        row.addWidget(self.cancel_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.refresh()

    def set_recommended(self, recommended, reason):
        self.badge.setVisible(recommended)
        if recommended:
            self.badge.setToolTip(reason)

    def refresh(self):
        ready, status = self.engine.is_ready()
        self.status.setText(status)
        self.setup_button.setEnabled(True)
        self.setup_button.setText("Set up again" if ready else "Set up")


class StillsWindow(ModeWindow):
    def __init__(self, state):
        super().__init__(state, "Stills", "stills", 950, 700)
        self._worker = None
        self._active_card = None
        self._force_ram = False
        # Thumbnail cache: (path, mtime, size) -> icon. _refresh
        # reuses the icon while the file is unchanged instead of
        # re-reading every picture from disk on every project change.
        self._thumb_cache = {}
        self._placeholder_icon = _placeholder_icon()
        self.engines = stills.discover_engines()
        self.fast_engine = self.engines[0]
        self.steady_engine = self.engines[1]

        self._ram_label = QLabel()
        self._ram_label.setProperty("role", "muted")
        self._ram_label.setWordWrap(True)
        self._refresh_ram()
        self.layout.addWidget(self._ram_label)
        # The memory line stays live, like the home window's machine
        # strip: free memory moves while a job runs.
        self._ram_timer = QTimer(self)
        self._ram_timer.setInterval(5000)
        self._ram_timer.timeout.connect(self._refresh_ram)
        self._ram_timer.start()

        cards = QHBoxLayout()
        self.fast_card = _EngineCard(
            self.fast_engine, lambda: self._setup_engine(self.fast_engine,
                                                         self.fast_card))
        self.steady_card = _EngineCard(
            self.steady_engine,
            lambda: self._setup_engine(self.steady_engine,
                                       self.steady_card))
        cards.addWidget(self.fast_card, 1)
        cards.addWidget(self.steady_card, 1)
        self.layout.addLayout(cards)

        list_label = QLabel("Pictures:")
        list_label.setProperty("role", "section")
        self.layout.addWidget(list_label)
        splitter = QSplitter(Qt.Horizontal)
        self.still_list = QListWidget()
        self.still_list.setIconSize(QSize(96, 96))
        # Three whole rows of 96 px thumbnails. Measured live: each
        # row is 115 px (96 icon + 19 list margins), so three rows
        # need 345 px of viewport; 350 covers the frame plus a
        # little slack for platform rounding.
        self.still_list.setMinimumHeight(350)
        self.still_list.setToolTip(
            "Pick a picture to see it large on the right.")
        self.still_list.currentRowChanged.connect(self._show_preview)
        splitter.addWidget(self.still_list)
        preview_wrap = QWidget()
        preview_layout = QVBoxLayout(preview_wrap)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        self.preview = _StillPreview()
        preview_layout.addWidget(self.preview, 1)
        self.preview_caption = QLabel("Pick a picture to see it large.")
        self.preview_caption.setProperty("role", "muted")
        self.preview_caption.setWordWrap(True)
        preview_layout.addWidget(self.preview_caption)
        splitter.addWidget(preview_wrap)
        splitter.setSizes([560, 340])
        self.layout.addWidget(splitter, 1)

        row = QHBoxLayout()
        self.make_button = DipButton("Make the missing pictures")
        self.make_button.setProperty("role", "primary")
        self.make_button.setToolTip(
            "Make pictures for the scenes that have none yet, and "
            "remake pictures whose scene changed since.")
        self.make_button.clicked.connect(self._make_missing)
        row.addWidget(self.make_button)
        self.remake_button = DipButton("Remake this picture")
        self.remake_button.setToolTip(
            "Make a new picture for the chosen scene, with a new "
            "seed.")
        self.remake_button.setEnabled(False)
        self.remake_button.clicked.connect(self._remake_still)
        row.addWidget(self.remake_button)
        seed_label = QLabel("Seed:")
        seed_label.setToolTip(
            "Type a seed number to get the same picture back, or "
            "leave it blank for a new random seed.")
        row.addWidget(seed_label)
        self.seed_edit = QLineEdit()
        self.seed_edit.setPlaceholderText("Random")
        self.seed_edit.setToolTip(
            "Type a seed number to remake this picture with that "
            "seed, or leave it blank for a new random seed.")
        self.seed_edit.setMaximumWidth(130)
        self.seed_edit.setClearButtonEnabled(True)
        row.addWidget(self.seed_edit)
        self.cancel_button = DipButton("Cancel")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self._cancel_job)
        row.addWidget(self.cancel_button)
        row.addStretch(1)
        self.layout.addLayout(row)
        self.make_note = QLabel()
        self.make_note.setProperty("role", "muted")
        self.make_note.setWordWrap(True)
        self.layout.addWidget(self.make_note)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.layout.addWidget(self.progress)
        self.finish_layout()
        state.project_changed.connect(_weak_slot(self, "_refresh"))
        self._refresh()

        QWidget.setTabOrder(self.fast_card.setup_button,
                            self.steady_card.setup_button)
        QWidget.setTabOrder(self.steady_card.setup_button,
                            self.still_list)
        QWidget.setTabOrder(self.still_list, self.make_button)

    def _refresh_ram(self):
        """The memory line, right now, formatted like the home
        window's machine strip ("about 407 MB", never "about 0 GB")."""
        self._ram_label.setText(
            f"Free memory right now: about "
            f"{_gb_text(machine.mem_available_mb())}. "
            "Each engine needs about 3 GB while it makes a picture. "
            "Pictures are made one at a time.")

    def _thumbnail_icon(self, still_path):
        """The 96 px icon for a picture, cached while the file is
        unchanged (same path, time, and size)."""
        try:
            stat = still_path.stat()
        except OSError:
            return QIcon(self._placeholder_icon)
        key = (str(still_path), stat.st_mtime_ns, stat.st_size)
        icon = self._thumb_cache.get(key)
        if icon is None:
            pixmap = QPixmap(str(still_path))
            icon = QIcon(pixmap.scaled(
                96, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self._thumb_cache[key] = icon
        return icon

    @staticmethod
    def _parse_seed(text):
        """A typed seed: an int, or None for a blank field (random).
        Raises ValueError when the text is not a whole number."""
        text = (text or "").strip()
        if not text:
            return None
        value = int(text)
        if value < 0:
            raise ValueError("a seed cannot be negative")
        return value

    def _show_preview(self, row):
        scenes = self.state.project.scenes
        project_dir = self.state.project_dir
        if 0 <= row < len(scenes) and scenes[row].still_seed:
            self.seed_edit.setPlaceholderText(
                f"Current seed {scenes[row].still_seed}")
        else:
            self.seed_edit.setPlaceholderText("Random")
        if 0 <= row < len(scenes):
            scene = scenes[row]
            if scene.still and project_dir is not None:
                still_path = project_dir / scene.still
                if still_path.exists():
                    self.preview.set_pixmap(QPixmap(str(still_path)))
                    caption = f"Scene {row + 1}: the picture."
                    if scene.still_seed:
                        caption += f" Seed {scene.still_seed}."
                    self.preview_caption.setText(caption)
                    self._update_remake_button()
                    return
            self.preview.set_pixmap(None)
            self.preview_caption.setText(
                f"Scene {row + 1}: no picture yet.")
        else:
            self.preview.set_pixmap(None)
            self.preview_caption.setText(
                "Pick a picture to see it large.")
        self._update_remake_button()

    def _update_remake_button(self):
        """Remake needs a chosen scene and a ready engine, and no
        job running."""
        row = self.still_list.currentRow()
        chosen = 0 <= row < len(self.state.project.scenes)
        self.remake_button.setEnabled(
            chosen and self._worker is None
            and stills.preferred_engine() is not None)

    def _stills_to_make(self):
        """How many pictures are missing or out of date."""
        project_dir = self.state.project_dir
        scenes = self.state.project.scenes
        if project_dir is None:
            return len(scenes)
        return sum(1 for scene in scenes
                   if scenes_mod.still_is_stale(scene, project_dir))

    def _refresh(self):
        bar = self.still_list.verticalScrollBar()
        scroll = bar.value()
        row = self.still_list.currentRow()
        self.fast_card.refresh()
        self.steady_card.refresh()
        engine, reason = stills.recommended_engine()
        self.fast_card.set_recommended(
            engine.key == self.fast_engine.key, reason)
        self.steady_card.set_recommended(
            engine.key == self.steady_engine.key, reason)
        self.still_list.clear()
        self._refresh_ram()
        project_dir = self.state.project_dir
        placeholder = self._placeholder_icon
        seen_keys = set()
        for index, scene in enumerate(self.state.project.scenes):
            if scene.still and project_dir is not None:
                still_path = project_dir / scene.still
                if still_path.exists():
                    icon = self._thumbnail_icon(still_path)
                    try:
                        stat = still_path.stat()
                        seen_keys.add((str(still_path),
                                       stat.st_mtime_ns, stat.st_size))
                    except OSError:
                        pass
                    if scenes_mod.still_is_stale(scene, project_dir):
                        mark = "picture out of date"
                    else:
                        mark = "picture ready"
                    item = QListWidgetItem(
                        icon, f"Scene {index + 1}: {mark}")
                    if scene.still_seed:
                        item.setToolTip(f"Seed {scene.still_seed}")
                    self.still_list.addItem(item)
                    continue
            item = QListWidgetItem(
                placeholder, f"Scene {index + 1}: no picture yet")
            self.still_list.addItem(item)
        # Drop cached icons for pictures no longer listed, so the
        # cache never holds a film's worth of dead thumbnails.
        self._thumb_cache = {
            key: icon for key, icon in self._thumb_cache.items()
            if key in seen_keys
        }
        if not self.state.project.scenes:
            # An empty list says what to do next instead of sitting
            # black and blank.
            hint = QListWidgetItem(
                "No scenes yet. Break the idea down in the Scenes "
                "window first.")
            hint.setFlags(Qt.NoItemFlags)
            self.still_list.addItem(hint)
        if (self.state.project.scenes
                and 0 <= row < self.still_list.count()):
            self.still_list.setCurrentRow(row)
        bar.setValue(scroll)
        todo = self._stills_to_make()
        self.make_button.setEnabled(
            todo > 0 and self._worker is None)
        self.make_note.setText(
            "All pictures are already made."
            if self.state.project.scenes and todo == 0 else "")
        self._show_preview(self.still_list.currentRow())

    def _setup_engine(self, engine, card):
        if not self.state.try_job(f"Setting up {engine.label.lower()}"):
            self.show_status(
                "error",
                f"Already busy: {self.state.job_owner}. Wait for it to "
                "finish, or cancel it.")
            return
        self._active_card = card
        card.progress.setVisible(True)
        card.progress.setRange(0, 0)
        card.cancel_button.setVisible(True)
        card.cancel_button.clicked.connect(self._cancel_job)
        card.setup_button.setEnabled(False)
        self.show_status("working", f"Setting up {engine.label.lower()}.")
        worker = Worker(lambda report, cancel: engine.setup(
            report=report, cancel_event=cancel),
            on_finish=self.state.finish_job)
        self._worker = worker
        worker.progress.connect(self._on_setup_progress)
        worker.done.connect(self._on_setup_done)
        worker.error.connect(self._on_setup_error)
        worker.start()

    def _on_setup_progress(self, label, fraction, detail):
        if self._active_card is None:
            return
        if fraction is None:
            self._active_card.progress.setRange(0, 0)
        else:
            self._active_card.progress.setRange(0, 100)
            self._active_card.progress.setValue(int(fraction * 100))
        text = label + (f": {detail}" if detail else "")
        self.show_status("working", text)

    def _finish_setup_ui(self):
        if self._active_card is not None:
            self._active_card.progress.setVisible(False)
            self._active_card.cancel_button.setVisible(False)
            try:
                self._active_card.cancel_button.clicked.disconnect(
                    self._cancel_job)
            except RuntimeError:
                pass
            self._active_card = None
        self._worker = None

    def _on_setup_done(self, _result):
        self._finish_setup_ui()
        self._refresh()
        self.show_status("done", "Engine ready.")

    def _on_setup_error(self, exc):
        self._finish_setup_ui()
        self._refresh()
        self.show_plain_error(exc)

    def _make_missing(self):
        project = self.state.project
        if not project.scenes:
            self.show_status(
                "idle", "There are no scenes yet. Break the idea into "
                "scenes first.")
            return
        if self._stills_to_make() == 0:
            self.show_status("idle", "All pictures are already made.")
            return
        engine = stills.preferred_engine()
        if engine is None:
            self.show_status(
                "idle",
                "No engine is ready yet. Set one up above first.")
            return
        try:
            engine.check_ram()
            self._force_ram = False
        except RamShortError as exc:
            if not self.ask_yes_no(
                    "Memory is short",
                    f"{exc}. Try anyway? The computer may slow down "
                    "while the pictures are made."):
                self.show_status("idle", "Left it. Free some memory "
                                 "and try again.")
                return
            self._force_ram = True
        if not self.state.try_job(f"Making pictures with {engine.label}"):
            self.show_status(
                "error",
                f"Already busy: {self.state.job_owner}. Wait for it to "
                "finish, or cancel it.")
            return
        self.show_status("working", "Making the missing pictures.")
        self.make_button.setEnabled(False)
        self.remake_button.setEnabled(False)
        self.cancel_button.setVisible(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        worker = Worker(lambda report, cancel: self._make_all(
            engine, report, cancel),
            on_finish=self.state.finish_job)
        self._worker = worker
        worker.progress.connect(self._on_make_progress)
        worker.done.connect(self._on_make_done)
        worker.error.connect(self._on_make_error)
        worker.start()

    def _make_all(self, engine, report, cancel):
        project = self.state.project
        project_dir = self.state.project_dir
        paths_map = scenes_mod.ensure_project_dirs(project_dir)
        scenes_mod.ensure_asset_ids(project.scenes)
        total = len(project.scenes)
        made = 0
        for index, scene in enumerate(project.scenes):
            if cancel.is_set():
                raise CancelledError()
            if not scenes_mod.still_is_stale(scene, project_dir):
                report("Making pictures", (index + 1) / total,
                       f"scene {index + 1} already has a picture")
                continue
            if not scene.prompt or not scene.prompt.strip():
                # A scene with no picture words cannot make a
                # picture: the empty prompt is never sent to the
                # engine. Work made before this scene is already
                # saved and kept.
                raise HicksFieldError(
                    f"Scene {index + 1} has no picture words yet.",
                    "Type the picture words for that scene in the "
                    "Scenes window, then try again.")
            name = scenes_mod.scene_still_name_for(scene, index)
            out = paths_map["stills"] / name
            # A remake keeps the recorded seed: only the prompt
            # changed, so the picture keeps its composition. A
            # first make gets a fresh explicit seed, recorded
            # below, so the picture can be made again.
            seed = (scene.still_seed if scene.still_seed
                    else scenes_mod.new_seed())
            scenes_mod.archive_previous_asset(out)
            engine.generate(scene.prompt, out, seed=seed,
                            cancel_event=cancel, force=self._force_ram)
            scene.still = f"{scenes_mod.STILLS_DIR}/{name}"
            scene.still_prompt = scene.prompt
            scene.still_seed = seed
            made += 1
            self.state.save()
            report("Making pictures", (index + 1) / total,
                   f"scene {index + 1} of {total}")
        return made

    def _remake_still(self):
        """Make a new picture for the chosen scene: with the typed
        seed when one is given, otherwise with a new random seed."""
        row = self.still_list.currentRow()
        scenes = self.state.project.scenes
        if not (0 <= row < len(scenes)):
            self.show_status("idle", "Pick a picture first.")
            return
        try:
            typed_seed = self._parse_seed(self.seed_edit.text())
        except ValueError:
            self.show_status(
                "idle",
                "That seed is not a number. Type a whole number, or "
                "leave the seed blank for a new random seed.")
            return
        engine = stills.preferred_engine()
        if engine is None:
            self.show_status(
                "idle",
                "No engine is ready yet. Set one up above first.")
            return
        try:
            engine.check_ram()
            self._force_ram = False
        except RamShortError as exc:
            if not self.ask_yes_no(
                    "Memory is short",
                    f"{exc}. Try anyway? The computer may slow down "
                    "while the picture is made."):
                self.show_status("idle", "Left it. Free some memory "
                                 "and try again.")
                return
            self._force_ram = True
        if not self.state.try_job(
                f"Remaking the picture for scene {row + 1}"):
            self.show_status(
                "error",
                f"Already busy: {self.state.job_owner}. Wait for it to "
                "finish, or cancel it.")
            return
        seed = (typed_seed if typed_seed is not None
                else scenes_mod.new_seed())
        self.show_status(
            "working", f"Remaking the picture for scene {row + 1}.")
        self.make_button.setEnabled(False)
        self.remake_button.setEnabled(False)
        self.cancel_button.setVisible(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        worker = Worker(
            lambda report, cancel: self._remake_one(
                engine, row, seed, report, cancel),
            on_finish=self.state.finish_job)
        self._worker = worker
        worker.progress.connect(self._on_make_progress)
        worker.done.connect(self._on_remake_done)
        worker.error.connect(self._on_make_error)
        worker.start()

    def _remake_one(self, engine, index, seed, report, cancel):
        project = self.state.project
        project_dir = self.state.project_dir
        paths_map = scenes_mod.ensure_project_dirs(project_dir)
        scenes_mod.ensure_asset_ids(project.scenes)
        scene = project.scenes[index]
        if cancel.is_set():
            raise CancelledError()
        if not scene.prompt or not scene.prompt.strip():
            # A scene with no picture words cannot make a picture:
            # the empty prompt is never sent to the engine.
            raise HicksFieldError(
                f"Scene {index + 1} has no picture words yet.",
                "Type the picture words for that scene in the "
                "Scenes window, then try again.")
        name = scenes_mod.scene_still_name_for(scene, index)
        out = paths_map["stills"] / name
        scenes_mod.archive_previous_asset(out)
        engine.generate(scene.prompt, out, seed=seed,
                        cancel_event=cancel, force=self._force_ram)
        scene.still = f"{scenes_mod.STILLS_DIR}/{name}"
        scene.still_prompt = scene.prompt
        scene.still_seed = seed
        self.state.save()
        report("Making pictures", 1.0, f"scene {index + 1} remade")
        return index

    def _on_make_progress(self, _label, fraction, detail):
        if fraction is not None:
            self.progress.setValue(int(fraction * 100))
        self.show_status("working", f"Making pictures. {detail}")

    def _finish_make_ui(self):
        self._worker = None
        self.make_button.setEnabled(True)
        self.cancel_button.setVisible(False)
        self.progress.setVisible(False)
        self._refresh()

    def _on_make_done(self, made):
        self._finish_make_ui()
        self.show_status("done", f"Made {plural(made, 'picture')}.")

    def _on_remake_done(self, index):
        self._finish_make_ui()
        self.show_status(
            "done", f"Remade the picture for scene {index + 1}.")

    def _on_make_error(self, exc):
        self._finish_make_ui()
        if isinstance(exc, CancelledError):
            self.show_status("idle", "Stopped. Pictures made so far "
                             "are kept.")
            return
        self.show_plain_error(exc)

    def _cancel_job(self):
        if self._worker is not None:
            self._worker.cancel()
            self.show_status("working", "Stopping.")


def _audio_output_available():
    """True when this computer has a speaker or headphones to play
    a line on. When the answer cannot be told, say yes: playback
    itself reports a failure plainly."""
    if not HAVE_MULTIMEDIA or QMediaDevices is None:
        return False
    try:
        return len(QMediaDevices().audioOutputs()) > 0
    except Exception:  # noqa: BLE001 - any probe failure means try
        return True


class VoiceWindow(ModeWindow):
    def __init__(self, state):
        super().__init__(state, "Voice", "voice", 850, 560)
        self._worker = None
        self._player = None
        self._audio = None
        self._playing_row = None
        self._play_buttons = []
        if HAVE_MULTIMEDIA:
            self._player = QMediaPlayer(self)
            self._audio = QAudioOutput(self)
            self._player.setAudioOutput(self._audio)
            self._player.mediaStatusChanged.connect(
                self._on_media_status)
            self._player.errorOccurred.connect(self._on_player_error)
        card = QFrame()
        card.setProperty("role", "card")
        card_layout = QVBoxLayout(card)
        name = QLabel("The voice")
        name.setProperty("role", "section")
        card_layout.addWidget(name)
        blurb = QLabel(
            "Pick the voice that reads the lines. Each voice is "
            "downloaded once and runs on this computer.")
        blurb.setProperty("role", "muted")
        blurb.setWordWrap(True)
        card_layout.addWidget(blurb)
        picker_row = QHBoxLayout()
        picker_label = QLabel("Voice:")
        picker_row.addWidget(picker_label)
        self.voice_combo = QComboBox()
        self.voice_combo.setToolTip(
            "Pick the voice that reads the lines.")
        for voice_id, entry in voice.VOICES.items():
            label = entry["label"]
            if entry["recommended"]:
                label += " (recommended)"
            self.voice_combo.addItem(label, voice_id)
        self.voice_combo.currentIndexChanged.connect(
            self._on_voice_picked)
        # Keys and the wheel can switch the voice with the picker
        # closed: the filter warns before those moves land.
        self.voice_combo.installEventFilter(self)
        picker_row.addWidget(self.voice_combo, 1)
        card_layout.addLayout(picker_row)
        self.voice_note = QLabel()
        self.voice_note.setProperty("role", "muted")
        self.voice_note.setWordWrap(True)
        card_layout.addWidget(self.voice_note)
        self.voice_status = QLabel()
        self.voice_status.setProperty("role", "muted")
        self.voice_status.setWordWrap(True)
        card_layout.addWidget(self.voice_status)
        self.voice_progress = QProgressBar()
        self.voice_progress.setVisible(False)
        card_layout.addWidget(self.voice_progress)
        row = QHBoxLayout()
        self.setup_button = DipButton("Set up the voice")
        self.setup_button.setToolTip(
            "Set up the voice so it can read the lines.")
        self.setup_button.clicked.connect(self._setup_voice)
        row.addWidget(self.setup_button)
        self.voice_cancel = DipButton("Cancel")
        self.voice_cancel.setVisible(False)
        self.voice_cancel.clicked.connect(self._cancel_job)
        row.addWidget(self.voice_cancel)
        row.addStretch(1)
        card_layout.addLayout(row)
        self.layout.addWidget(card)

        list_label = QLabel("Spoken lines:")
        list_label.setProperty("role", "section")
        self.layout.addWidget(list_label)
        self.line_list = QListWidget()
        self.layout.addWidget(self.line_list, 1)

        row = QHBoxLayout()
        self.make_button = DipButton("Make the spoken lines")
        self.make_button.setProperty("role", "primary")
        self.make_button.setToolTip(
            "Speak the lines that have none yet, and speak again the "
            "lines whose words or voice changed.")
        self.make_button.clicked.connect(self._make_lines)
        row.addWidget(self.make_button)
        self.cancel_button = DipButton("Cancel")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self._cancel_job)
        row.addWidget(self.cancel_button)
        row.addStretch(1)
        self.layout.addLayout(row)
        self.make_note = QLabel()
        self.make_note.setProperty("role", "muted")
        self.make_note.setWordWrap(True)
        self.layout.addWidget(self.make_note)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.layout.addWidget(self.progress)
        self.finish_layout()
        state.project_changed.connect(_weak_slot(self, "_refresh"))
        self._refresh()

        QWidget.setTabOrder(self.voice_combo, self.setup_button)
        QWidget.setTabOrder(self.setup_button, self.line_list)
        QWidget.setTabOrder(self.line_list, self.make_button)

    def closeEvent(self, event):
        # Stop any line playing before the window goes away, so the
        # player is never left running past teardown.
        self._stop_line()
        super().closeEvent(event)

    def _update_voice_note(self, voice_id):
        """The picked voice's description and download size."""
        entry = voice.VOICES.get(voice_id)
        if entry is None:
            self.voice_note.setText("")
            return
        total = sum(info["size"] for info in entry["files"].values())
        self.voice_note.setText(
            f"{entry['description']} Download is about "
            f"{format_bytes(total)}.")

    def eventFilter(self, watched, event):
        # Keys and the wheel change the voice while the picker is
        # closed, with no popup in between: warn here, before the
        # picker moves, the way _on_voice_picked warns for a pick
        # from the open popup.
        if (watched is self.voice_combo
                and not self.voice_combo.view().isVisible()
                and self._picker_move_would_switch(event)
                and not self._voice_switch_ok()):
            return True
        return super().eventFilter(watched, event)

    def _picker_move_would_switch(self, event):
        """True when this key or wheel event would move the voice
        picker to another voice."""
        index = self.voice_combo.currentIndex()
        count = self.voice_combo.count()
        if event.type() == QEvent.KeyPress:
            key = event.key()
            if key in (Qt.Key_Up, Qt.Key_PageUp):
                return index > 0
            if key in (Qt.Key_Down, Qt.Key_PageDown):
                return index < count - 1
            if key == Qt.Key_Home:
                return index != 0
            if key == Qt.Key_End:
                return index != count - 1
            return False
        if event.type() == QEvent.Wheel:
            delta = event.angleDelta().y()
            if delta == 0:
                delta = event.pixelDelta().y()
            if delta > 0:
                return index > 0
            if delta < 0:
                return index < count - 1
            return False
        return False

    def _fresh_spoken_lines(self):
        """How many spoken lines match the current voice: the lines
        a voice switch would make out of date."""
        project_dir = self.state.project_dir
        voice_id = self.state.project.voice
        if project_dir is None:
            return 0
        return sum(
            1 for scene in self.state.project.scenes
            if not scenes_mod.voice_is_stale(scene, project_dir,
                                             voice_id))

    def _voice_switch_ok(self):
        """True when switching voice may go ahead: either no spoken
        line would go out of date, or the user said yes after being
        warned how many would."""
        count = self._fresh_spoken_lines()
        if count == 0:
            return True
        return self.ask_yes_no(
            "Switch voice?",
            f"Switching voice will make {plural(count, 'spoken line')} "
            "out of date. They will need speaking again. "
            "Switch anyway?")

    def _on_voice_picked(self, _index):
        voice_id = self.voice_combo.currentData()
        if not voice_id or voice_id not in voice.VOICES:
            return
        self._update_voice_note(voice_id)
        if voice_id == self.state.project.voice:
            return
        if self.voice_combo.view().isVisible():
            # The picker is open: a person just picked this voice.
            # Warn before the switch lands. A change with the picker
            # closed comes from the keyboard or the wheel (warned in
            # the event filter) or from code (no warning needed).
            if not self._voice_switch_ok():
                self._refresh()
                return
        # A new voice makes the spoken lines out of date: they were
        # spoken in the old voice. The refresh after this marks
        # them, and the next make speaks them again.
        self.state.project.voice = voice_id
        self.state.save()
        self.state.project_changed.emit()

    def _on_play_clicked(self):
        button = self.sender()
        row = button.property("row")
        if row is not None:
            self._play_line(int(row))

    def _play_line(self, row):
        if self._playing_row == row:
            self._stop_line()
            return
        self._stop_line()
        scenes = self.state.project.scenes
        project_dir = self.state.project_dir
        if not (0 <= row < len(scenes)) or project_dir is None:
            return
        scene = scenes[row]
        if not scene.audio:
            return
        audio_path = project_dir / scene.audio
        if not audio_path.exists():
            return
        if not HAVE_MULTIMEDIA:
            self.show_status(
                "idle",
                "Playing lines needs the media pieces, which this "
                "build does not have.")
            return
        if not _audio_output_available():
            # No speaker or headphones: starting playback anyway
            # hangs the app at teardown on such a machine (the
            # player never closes). Say so plainly instead.
            self.show_status(
                "idle",
                "No speaker or headphones were found on this "
                "computer, so the line cannot play here.")
            return
        self._player.setSource(QUrl.fromLocalFile(str(audio_path)))
        self._audio.setVolume(1.0)
        self._playing_row = row
        self._player.play()
        self._mark_playing_button()
        self.show_status("working", f"Playing scene {row + 1}.")

    def _stop_line(self):
        if self._player is not None:
            self._player.stop()
        self._playing_row = None
        self._mark_playing_button()

    def _mark_playing_button(self):
        for index, button in enumerate(self._play_buttons):
            button.setText(
                "Stop" if index == self._playing_row else "Play")

    def _on_media_status(self, status):
        if status == QMediaPlayer.EndOfMedia:
            self._playing_row = None
            self._mark_playing_button()
            self.show_status("idle", "Ready.")

    def _on_player_error(self, _error, _message):
        self._playing_row = None
        self._mark_playing_button()
        self.show_status(
            "idle", "That line could not play here. The file is still "
            "in the film folder.")

    def _refresh(self):
        bar = self.line_list.verticalScrollBar()
        scroll = bar.value()
        row = self.line_list.currentRow()
        voice_id = self.state.project.voice
        if voice_id not in voice.VOICES:
            # A project naming a voice this HicksField does not
            # know falls back to the usual voice, and the project
            # is fixed so the fallback sticks.
            voice_id = voice.VOICE_ID
            self.state.project.voice = voice_id
            self.state.save()
        index = self.voice_combo.findData(voice_id)
        self.voice_combo.blockSignals(True)
        self.voice_combo.setCurrentIndex(index if index >= 0 else 0)
        self.voice_combo.blockSignals(False)
        self._update_voice_note(voice_id)
        ready, status = voice.is_ready(voice_id)
        self.voice_status.setText(status)
        self.setup_button.setText("Set up again" if ready
                                  else "Set up the voice")
        self.line_list.clear()
        self._play_buttons = []
        project_dir = self.state.project_dir
        for index, scene in enumerate(self.state.project.scenes):
            done = (scene.audio and project_dir is not None
                    and (project_dir / scene.audio).exists())
            text = scene.narration or "(no spoken line yet)"
            if done and project_dir is not None and \
                    scenes_mod.voice_is_stale(scene, project_dir,
                                              voice_id):
                state_text = "out of date"
            elif done:
                state_text = "spoken"
            else:
                state_text = "not spoken yet"
            row_widget = QWidget()
            row_widget.setAttribute(Qt.WA_TransparentForMouseEvents)
            # Fixed height, like the film-frame rows: the item
            # widget's sizeHint is not to be trusted here (the
            # word-wrapped label makes it collapse to one line and
            # the Play button with it).
            row_widget.setFixedHeight(44)
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(4, 2, 4, 2)
            label = QLabel(
                f"Scene {index + 1}: {text[:80]} [{state_text}]")
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
            label.setWordWrap(True)
            row_layout.addWidget(label, 1)
            play_button = DipButton("Play")
            play_button.setProperty("row", index)
            play_button.setEnabled(bool(done) and HAVE_MULTIMEDIA)
            play_button.setToolTip(
                "Hear this line before the film is built.")
            play_button.clicked.connect(self._on_play_clicked)
            row_layout.addWidget(play_button)
            self._play_buttons.append(play_button)
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 44))
            self.line_list.addItem(item)
            self.line_list.setItemWidget(item, row_widget)
        if 0 <= row < self.line_list.count():
            self.line_list.setCurrentRow(row)
        bar.setValue(scroll)
        todo = self._lines_to_make()
        self.make_button.setEnabled(
            todo > 0 and self._worker is None)
        self.make_note.setText(
            "All spoken lines are already made."
            if self.state.project.scenes and todo == 0 else "")
        self._mark_playing_button()

    def _lines_to_make(self):
        """How many spoken lines are missing or out of date."""
        project_dir = self.state.project_dir
        scenes = self.state.project.scenes
        voice_id = self.state.project.voice
        if project_dir is None:
            return len(scenes)
        return sum(1 for scene in scenes
                   if scenes_mod.voice_is_stale(scene, project_dir,
                                                voice_id))

    def _needs_voice(self):
        """True when some spoken line still needs a voice: a line
        with words in it that is missing or out of date. Empty lines
        are silence and need no voice."""
        project_dir = self.state.project_dir
        voice_id = self.state.project.voice
        if project_dir is None:
            return any(scene.narration.strip()
                       for scene in self.state.project.scenes)
        return any(scene.narration.strip()
                   and scenes_mod.voice_is_stale(scene, project_dir,
                                                 voice_id)
                   for scene in self.state.project.scenes)

    def _setup_voice(self):
        if not self.state.try_job("Setting up the voice"):
            self.show_status(
                "error",
                f"Already busy: {self.state.job_owner}. Wait for it to "
                "finish, or cancel it.")
            return
        self.voice_progress.setVisible(True)
        self.voice_progress.setRange(0, 0)
        self.voice_cancel.setVisible(True)
        self.setup_button.setEnabled(False)
        self.show_status("working", "Setting up the voice.")
        worker = Worker(lambda report, cancel: voice.setup(
            report=report, cancel_event=cancel,
            voice_id=self.state.project.voice),
            on_finish=self.state.finish_job)
        self._worker = worker
        worker.progress.connect(self._on_setup_progress)
        worker.done.connect(self._on_setup_done)
        worker.error.connect(self._on_setup_error)
        worker.start()

    def _on_setup_progress(self, label, fraction, detail):
        if fraction is None:
            self.voice_progress.setRange(0, 0)
        else:
            self.voice_progress.setRange(0, 100)
            self.voice_progress.setValue(int(fraction * 100))
        text = label + (f": {detail}" if detail else "")
        self.show_status("working", text)

    def _finish_setup_ui(self):
        self._worker = None
        self.voice_progress.setVisible(False)
        self.voice_cancel.setVisible(False)
        self.setup_button.setEnabled(True)
        self._refresh()

    def _on_setup_done(self, _result):
        self._finish_setup_ui()
        self.show_status("done", "Voice ready.")

    def _on_setup_error(self, exc):
        self._finish_setup_ui()
        self.show_plain_error(exc)

    def _make_lines(self):
        if not self.state.project.scenes:
            self.show_status(
                "idle", "There are no scenes yet. Break the idea into "
                "scenes first.")
            return
        if self._lines_to_make() == 0:
            self.show_status(
                "idle", "All spoken lines are already made.")
            return
        if self._needs_voice():
            ready, _status = voice.is_ready(self.state.project.voice)
            if not ready:
                self.show_status(
                    "idle", "Set up the voice first.")
                return
        if not self.state.try_job("Speaking the lines"):
            self.show_status(
                "error",
                f"Already busy: {self.state.job_owner}. Wait for it to "
                "finish, or cancel it.")
            return
        self.show_status("working", "Speaking the lines.")
        self.make_button.setEnabled(False)
        self.cancel_button.setVisible(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        worker = Worker(self._make_all,
                        on_finish=self.state.finish_job)
        self._worker = worker
        worker.progress.connect(self._on_make_progress)
        worker.done.connect(self._on_make_done)
        worker.error.connect(self._on_make_error)
        worker.start()

    def _make_all(self, report, cancel):
        project = self.state.project
        project_dir = self.state.project_dir
        paths_map = scenes_mod.ensure_project_dirs(project_dir)
        scenes_mod.ensure_asset_ids(project.scenes)
        total = len(project.scenes)
        made = 0
        for index, scene in enumerate(project.scenes):
            if cancel.is_set():
                raise CancelledError()
            if not scenes_mod.voice_is_stale(scene, project_dir,
                                             project.voice):
                report("Speaking the lines", (index + 1) / total,
                       f"scene {index + 1} already spoken")
                continue
            name = scenes_mod.scene_audio_name_for(scene, index)
            out = paths_map["audio"] / name
            voice.synthesize(scene.narration, out, cancel_event=cancel,
                             voice_id=project.voice)
            scene.audio = f"{scenes_mod.AUDIO_DIR}/{name}"
            scene.voice_text = scene.narration
            scene.voice_id = project.voice
            made += 1
            self.state.save()
            report("Speaking the lines", (index + 1) / total,
                   f"scene {index + 1} of {total}")
        return made

    def _on_make_progress(self, _label, fraction, detail):
        if fraction is not None:
            self.progress.setValue(int(fraction * 100))
        self.show_status("working", f"Speaking the lines. {detail}")

    def _finish_make_ui(self):
        self._worker = None
        self.make_button.setEnabled(True)
        self.cancel_button.setVisible(False)
        self.progress.setVisible(False)
        self._refresh()

    def _on_make_done(self, made):
        self._finish_make_ui()
        self.show_status("done", f"Spoke {plural(made, 'line')}.")

    def _on_make_error(self, exc):
        self._finish_make_ui()
        if isinstance(exc, CancelledError):
            self.show_status("idle", "Stopped. Lines spoken so far "
                             "are kept.")
            return
        self.show_plain_error(exc)

    def _cancel_job(self):
        if self._worker is not None:
            self._worker.cancel()
            self.show_status("working", "Stopping.")


class ExportWindow(ModeWindow):
    # The ffmpeg install runs in its own thread; its callbacks reach
    # the window through these signals, never by touching widgets
    # from the install thread.
    install_progress = Signal(str)
    install_done = Signal()
    install_error = Signal(str)

    def __init__(self, state):
        super().__init__(state, "Export", "export", 850, 600)
        self._worker = None
        self._force_ram = False
        self._started = None
        self._ffmpeg_installing = False
        self.install_progress.connect(self._on_install_progress)
        self.install_done.connect(self._on_install_done)
        self.install_error.connect(self._on_install_error)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._player = None
        self._audio = None
        self._film_source = None
        self._duration = 0
        # The poster: the film's first frame, held in memory once
        # taken. _poster_film names the film it came from (path,
        # size, changed time), so a rebuilt film gets a fresh poster
        # and an unchanged film is never re-extracted.
        self._poster_film = None
        self._poster_pixmap = None
        # True when the film exists but its first frame could not be
        # taken: the player area says so instead of staying black.
        self._poster_failed = False
        if HAVE_MULTIMEDIA:
            self._player = QMediaPlayer(self)
            self._audio = QAudioOutput(self)
            self._player.setAudioOutput(self._audio)
            self._player.durationChanged.connect(
                self._on_duration_changed)
            self._player.positionChanged.connect(
                self._on_position_changed)
            self._player.mediaStatusChanged.connect(
                self._on_media_status)
            self._player.errorOccurred.connect(self._on_player_error)

        summary = QLabel("What is ready:")
        summary.setProperty("role", "section")
        self.layout.addWidget(summary)
        self.summary_label = QLabel()
        self.summary_label.setProperty("role", "muted")
        self.summary_label.setWordWrap(True)
        self.layout.addWidget(self.summary_label)
        self.ffmpeg_label = QLabel()
        self.ffmpeg_label.setProperty("role", "muted")
        self.ffmpeg_label.setWordWrap(True)
        ffmpeg_row = QHBoxLayout()
        ffmpeg_row.addWidget(self.ffmpeg_label, 1)
        self.install_ffmpeg_button = DipButton("Install ffmpeg")
        self.install_ffmpeg_button.setToolTip(
            "Install ffmpeg. The computer asks for permission in a "
            "window.")
        self.install_ffmpeg_button.setVisible(False)
        self.install_ffmpeg_button.clicked.connect(
            self._install_ffmpeg)
        ffmpeg_row.addWidget(self.install_ffmpeg_button)
        self.layout.addLayout(ffmpeg_row)

        build_label = QLabel("Build the film:")
        build_label.setProperty("role", "section")
        self.layout.addWidget(build_label)
        self.engine_note = QLabel()
        self.engine_note.setProperty("role", "muted")
        self.engine_note.setWordWrap(True)
        self.layout.addWidget(self.engine_note)
        self.open_stills_button = DipButton("Open Stills")
        self.open_stills_button.setToolTip(
            "Open the Stills window to set up a picture engine.")
        self.open_stills_button.clicked.connect(
            lambda: self.state.open_window_requested.emit("stills"))
        self.open_stills_button.setVisible(False)
        stills_row = QHBoxLayout()
        stills_row.addWidget(self.open_stills_button)
        stills_row.addStretch(1)
        self.layout.addLayout(stills_row)
        row = QHBoxLayout()
        self.fast_build = DipButton("Build with the fast engine")
        self.fast_build.setProperty("role", "primary")
        self.fast_build.setToolTip(
            "Build the film, making pictures with the fast engine.")
        self.fast_build.clicked.connect(
            lambda: self._build(stills.discover_engines()[0]))
        row.addWidget(self.fast_build)
        self.steady_build = DipButton("Build with the steady engine")
        self.steady_build.setProperty("role", "curtain")
        self.steady_build.setToolTip(
            "Build the film, making pictures with the steady engine.")
        self.steady_build.clicked.connect(
            lambda: self._build(stills.discover_engines()[1]))
        row.addWidget(self.steady_build)
        self.cancel_button = DipButton("Cancel")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self._cancel_job)
        row.addWidget(self.cancel_button)
        row.addStretch(1)
        self.layout.addLayout(row)

        self.step_label = QLabel()
        self.step_label.setProperty("role", "muted")
        self.layout.addWidget(self.step_label)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.layout.addWidget(self.progress)
        self.elapsed_label = QLabel()
        self.elapsed_label.setProperty("role", "muted")
        self.layout.addWidget(self.elapsed_label)

        result_label = QLabel("The finished film:")
        result_label.setProperty("role", "section")
        self.layout.addWidget(result_label)
        result_row = QHBoxLayout()
        self.badge = FinishBadge()
        result_row.addWidget(self.badge)
        self.film_label = QLabel("No film built yet.")
        self.film_label.setProperty("role", "muted")
        self.film_label.setWordWrap(True)
        result_row.addWidget(self.film_label, 1)
        self.layout.addLayout(result_row)
        watch_row = QHBoxLayout()
        self.watch_button = DipButton("Open the film")
        self.watch_button.setEnabled(False)
        self.watch_button.setToolTip(
            "Open the finished film in the usual video player.")
        self.watch_button.clicked.connect(self._open_film)
        watch_row.addWidget(self.watch_button)
        self.folder_button = DipButton("Open the folder")
        self.folder_button.setEnabled(False)
        self.folder_button.setToolTip(
            "Open the film folder in the file manager.")
        self.folder_button.clicked.connect(self._open_folder)
        watch_row.addWidget(self.folder_button)
        watch_row.addStretch(1)
        self.layout.addLayout(watch_row)
        self.player_box = QWidget()
        player_layout = QVBoxLayout(self.player_box)
        player_layout.setContentsMargins(0, 0, 0, 0)
        if HAVE_MULTIMEDIA:
            self.video_widget = QVideoWidget()
            self._player.setVideoOutput(self.video_widget)
        else:
            self.video_widget = QLabel(
                "The film preview needs the media pieces, which this "
                "build does not have.")
            self.video_widget.setProperty("role", "muted")
            self.video_widget.setWordWrap(True)
        self.video_widget.setMinimumHeight(240)
        # The poster sits in the same spot as the player: the film's
        # first frame shows while idle, and the player takes over
        # when Play is pressed. The poster works even without the
        # media pieces, since it is just a picture.
        self.poster_label = _StillPreview()
        self.poster_label.setMinimumHeight(240)
        self.poster_label.setToolTip("The first frame of the film.")
        # The note shown when the film is fine but its first frame
        # could not be taken: the player area must say so instead
        # of staying black and silent.
        self.poster_note = QLabel(
            "No preview frame. The film itself is fine.")
        self.poster_note.setProperty("role", "muted")
        self.poster_note.setWordWrap(True)
        self.poster_note.setAlignment(Qt.AlignCenter)
        self.poster_note.setMinimumHeight(240)
        self._player_stack = QStackedWidget()
        self._player_stack.addWidget(self.video_widget)
        self._player_stack.addWidget(self.poster_label)
        self._player_stack.addWidget(self.poster_note)
        player_layout.addWidget(self._player_stack)
        controls = QHBoxLayout()
        self.play_button = DipButton("Play")
        self.play_button.setEnabled(False)
        self.play_button.setToolTip("Watch the film here.")
        self.play_button.clicked.connect(self._toggle_play)
        controls.addWidget(self.play_button)
        self.scrub = QSlider(Qt.Horizontal)
        self.scrub.setEnabled(False)
        self.scrub.setToolTip("Drag to jump to a part of the film.")
        self.scrub.sliderMoved.connect(self._on_scrub_moved)
        controls.addWidget(self.scrub, 1)
        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setProperty("role", "muted")
        controls.addWidget(self.time_label)
        player_layout.addLayout(controls)
        self.layout.addWidget(self.player_box)
        self.skipped_text = QTextEdit()
        self.skipped_text.setReadOnly(True)
        self.skipped_text.setMaximumHeight(90)
        self.skipped_text.setVisible(False)
        self.layout.addWidget(self.skipped_text)
        self.finish_layout()
        state.project_changed.connect(_weak_slot(self, "_refresh"))
        self._refresh()

        QWidget.setTabOrder(self.fast_build, self.steady_build)
        QWidget.setTabOrder(self.steady_build, self.play_button)
        QWidget.setTabOrder(self.play_button, self.scrub)
        QWidget.setTabOrder(self.scrub, self.watch_button)
        QWidget.setTabOrder(self.watch_button, self.folder_button)

    def _set_film_source(self, film):
        if not HAVE_MULTIMEDIA or self._player is None:
            return
        if self._film_source == str(film):
            return
        self._film_source = str(film)
        self._player.setSource(QUrl.fromLocalFile(str(film)))
        self.play_button.setEnabled(True)
        self.scrub.setEnabled(True)

    def _toggle_play(self):
        if not HAVE_MULTIMEDIA or self._player is None:
            return
        if self._player.playbackState() == QMediaPlayer.PlayingState:
            self._player.pause()
            self.play_button.setText("Play")
        else:
            self._show_video()
            self._player.play()
            self.play_button.setText("Pause")

    def _is_playing(self):
        return (HAVE_MULTIMEDIA and self._player is not None
                and self._player.playbackState()
                == QMediaPlayer.PlayingState)

    def _show_poster(self):
        """Show the film's first frame. When the frame could not be
        taken, say so in the player area instead of leaving it black
        and silent; with no film at all, the player surface shows."""
        if self._poster_pixmap is not None:
            self.poster_label.set_pixmap(self._poster_pixmap)
            self._player_stack.setCurrentWidget(self.poster_label)
        elif self._poster_failed:
            self._player_stack.setCurrentWidget(self.poster_note)
        else:
            self._player_stack.setCurrentWidget(self.video_widget)

    def _show_video(self):
        self._player_stack.setCurrentWidget(self.video_widget)

    def _update_poster(self, film):
        """Hold the film's first frame as the poster, taking it once
        per film. A film that cannot give up a frame gets a plain
        note in the player area. While the film plays, the player
        stays in front: the poster is for idle only."""
        if film is None or not film.exists():
            self._poster_film = None
            self._poster_pixmap = None
            self._poster_failed = False
            self._show_video()
            return
        try:
            stat = film.stat()
        except OSError:
            return
        key = (str(film), stat.st_size, stat.st_mtime_ns)
        if key != self._poster_film:
            self._poster_film = key
            self._poster_pixmap = None
            self._poster_failed = False
            # The frame is taken into the film's own folder, where
            # there is room, and removed once the picture is held.
            tmp_path = film.with_name(f".{film.stem}-poster.png")
            try:
                taken = video.extract_first_frame(film, tmp_path)
                if taken is not None:
                    pixmap = QPixmap(str(taken))
                    if not pixmap.isNull():
                        self._poster_pixmap = pixmap
            finally:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            self._poster_failed = self._poster_pixmap is None
        if not self._is_playing():
            self._show_poster()

    def _on_media_status(self, status):
        if status == QMediaPlayer.EndOfMedia:
            self.play_button.setText("Play")
            self._show_poster()

    def _on_duration_changed(self, duration):
        self._duration = duration
        self.scrub.setRange(0, max(0, duration))
        self._update_time_label(self._player.position())

    def _on_position_changed(self, position):
        if not self.scrub.isSliderDown():
            self.scrub.blockSignals(True)
            self.scrub.setValue(position)
            self.scrub.blockSignals(False)
        self._update_time_label(position)
        if self._player.playbackState() != QMediaPlayer.PlayingState:
            self.play_button.setText("Play")

    def _on_scrub_moved(self, position):
        if HAVE_MULTIMEDIA and self._player is not None:
            self._player.setPosition(position)
            self._update_time_label(position)

    def _update_time_label(self, position):
        self.time_label.setText(
            f"{_format_clock(position / 1000)} / "
            f"{_format_clock(self._duration / 1000)}")

    def _on_player_error(self, _error, _message):
        self.play_button.setText("Play")
        self._show_poster()
        self.show_status(
            "idle", "The film preview could not start here. "
            "Use Open the film instead.")

    def _refresh_summary(self):
        """The "What is ready" counts, from the project as it is now.
        Called on every refresh, and from build progress, so the
        summary moves as the build lands pieces."""
        project = self.state.project
        project_dir = self.state.project_dir
        count = len(project.scenes)
        stills_ready = voice_ready = clips_ready = 0
        voice_id = project.voice
        if project_dir is not None:
            for index, scene in enumerate(project.scenes):
                # Out of date counts as to make, not ready.
                if not scenes_mod.still_is_stale(scene, project_dir):
                    stills_ready += 1
                if not scenes_mod.voice_is_stale(scene, project_dir,
                                                 voice_id):
                    voice_ready += 1
                motion = video.motion_for_scene(index, scene.motion)
                if not scenes_mod.clip_is_stale(scene, project_dir,
                                                voice_id, motion):
                    clips_ready += 1
        self.summary_label.setText(
            f"{plural(count, 'scene')}. "
            f"{plural(stills_ready, 'picture')} ready. "
            f"{plural(voice_ready, 'spoken line')} ready. "
            f"{plural(clips_ready, 'moving clip')} ready.")

    def _stills_needed(self):
        """True when some picture still needs making."""
        project_dir = self.state.project_dir
        if project_dir is None:
            return bool(self.state.project.scenes)
        return any(scenes_mod.still_is_stale(scene, project_dir)
                   for scene in self.state.project.scenes)

    def _voice_needed(self):
        """True when some spoken line still needs a voice: a line
        with words in it that is missing or out of date. A film with
        no spoken lines needs no voice."""
        project_dir = self.state.project_dir
        voice_id = self.state.project.voice
        if project_dir is None:
            return any(scene.narration.strip()
                       for scene in self.state.project.scenes)
        return any(scene.narration.strip()
                   and scenes_mod.voice_is_stale(scene, project_dir,
                                                 voice_id)
                   for scene in self.state.project.scenes)

    @staticmethod
    def _engine_word(engine):
        return "fast" if engine.key == "fastsd" else "steady"

    def _build_button_tip(self, engine, ready, stills_needed,
                          count, version, building):
        """Why this build button is greyed out, or what it does when
        it is not: the reason lives on the button itself."""
        if building:
            return "A build is already running."
        if count == 0:
            return ("There are no scenes yet. Break the idea into "
                    "scenes first.")
        if version is None:
            return ("ffmpeg is missing. Use the Install ffmpeg "
                    "button above.")
        if not ready and stills_needed:
            return (f"The {self._engine_word(engine)} engine is not "
                    "set up yet. Open Stills and press Set up.")
        return (f"Build the film, making pictures with the "
                f"{self._engine_word(engine)} engine.")

    def _refresh(self):
        self._refresh_summary()
        project = self.state.project
        project_dir = self.state.project_dir
        count = len(project.scenes)
        version = video.ffmpeg_version_short()
        if version:
            self.ffmpeg_label.setText(
                f"ffmpeg is ready (version {version})")
            self.install_ffmpeg_button.setVisible(False)
        else:
            self.ffmpeg_label.setText(
                "ffmpeg is missing. It is needed to build the film.")
            self.install_ffmpeg_button.setVisible(True)
            self.install_ffmpeg_button.setEnabled(
                not self._ffmpeg_installing)
        engines = stills.discover_engines()
        fast_ready, _s = engines[0].is_ready()
        steady_ready, _s = engines[1].is_ready()
        stills_needed = self._stills_needed()
        building = self._worker is not None
        if not building:
            # A build button is enabled when its engine is ready, or
            # when no picture needs making: pictures already ready
            # are not blocked by an engine that is not needed.
            # While a build runs, the buttons stay as _build left
            # them: a refresh mid-build must not switch them on.
            self.fast_build.setEnabled(
                bool(count) and version is not None
                and (fast_ready or not stills_needed))
            self.steady_build.setEnabled(
                bool(count) and version is not None
                and (steady_ready or not stills_needed))
        self.fast_build.setToolTip(
            self._build_button_tip(engines[0], fast_ready,
                                   stills_needed, count, version,
                                   building))
        self.steady_build.setToolTip(
            self._build_button_tip(engines[1], steady_ready,
                                   stills_needed, count, version,
                                   building))
        engine, reason = stills.recommended_engine()
        if not fast_ready and not steady_ready:
            # No engine at all: say what is missing and where to fix
            # it, with a button that goes there.
            if stills_needed:
                self.engine_note.setText(
                    "No picture engine is set up yet. Open Stills "
                    "and press Set up.")
            else:
                self.engine_note.setText(
                    "No picture engine is set up yet, but every "
                    "picture is already made, so the film can still "
                    "be built. To make new pictures, open Stills "
                    "and press Set up.")
            self.open_stills_button.setVisible(True)
        else:
            self.engine_note.setText(reason)
            self.open_stills_button.setVisible(False)
        self.fast_build.setText(
            "Build with the fast engine (recommended)"
            if engine.key == engines[0].key
            else "Build with the fast engine")
        self.steady_build.setText(
            "Build with the steady engine (recommended)"
            if engine.key == engines[1].key
            else "Build with the steady engine")
        film = (project_dir / scenes_mod.FILM_FILE
                if project_dir is not None else None)
        if film is not None and film.exists():
            self.film_label.setText(f"Film: {film}")
            self.watch_button.setEnabled(True)
            self.folder_button.setEnabled(True)
            self._set_film_source(film)
            self._update_poster(film)
        else:
            self.film_label.setText("No film built yet.")
            self.watch_button.setEnabled(False)
            self.folder_button.setEnabled(False)
            self.play_button.setEnabled(False)
            self._update_poster(None)

    def _install_ffmpeg(self):
        """One-click ffmpeg install: permission is asked in a
        window, and the install runs in the background."""
        if self._ffmpeg_installing:
            return
        self._ffmpeg_installing = True
        self.install_ffmpeg_button.setEnabled(False)
        self.show_status("working", "Installing ffmpeg.")
        sysdeps.install_packages_async(
            ["ffmpeg"],
            progress_cb=self.install_progress.emit,
            done_cb=self.install_done.emit,
            error_cb=self.install_error.emit)

    def _on_install_progress(self, message):
        self.show_status("working", message)

    def _on_install_done(self):
        self._ffmpeg_installing = False
        self._refresh()
        self.show_status("done", "ffmpeg is installed.")

    def _on_install_error(self, message):
        self._ffmpeg_installing = False
        self._refresh()
        self.show_status(
            "idle", f"ffmpeg was not installed: {message}.")

    def _build(self, engine):
        project = self.state.project
        if not project.scenes:
            self.show_status(
                "idle", "There are no scenes yet. Break the idea into "
                "scenes first.")
            return
        stills_needed = self._stills_needed()
        ready, _status = engine.is_ready()
        if not ready and stills_needed:
            # The engine is only needed when a picture still needs
            # making; pictures already ready build without it.
            self.show_status(
                "idle",
                f"The {self._engine_word(engine)} engine is not set "
                "up yet. Open Stills and press Set up.")
            return
        if self._voice_needed():
            # A film with no spoken lines needs no voice: empty
            # lines are silence, and lines already spoken are kept.
            voice_ready, _s = voice.is_ready(project.voice)
            if not voice_ready:
                self.show_status(
                    "idle",
                    "Set up the voice first, in the Voice window.")
                return
        if video.find_ffmpeg() is None:
            self.show_status(
                "error",
                "ffmpeg is missing. Use the Install ffmpeg button "
                "above.")
            return
        if not stills_needed:
            # No picture to make: the engine runs nothing, so its
            # memory is not needed either.
            self._force_ram = True
        else:
            try:
                engine.check_ram()
                self._force_ram = False
            except RamShortError as exc:
                if not self.ask_yes_no(
                        "Memory is short",
                        f"{exc}. Build anyway? The computer may slow "
                        "down while the film is built."):
                    self.show_status("idle", "Left it. Free some "
                                     "memory and try again.")
                    return
                self._force_ram = True
        if not self.state.try_job("Building the film"):
            self.show_status(
                "error",
                f"Already busy: {self.state.job_owner}. Wait for it to "
                "finish, or cancel it.")
            return
        self.badge.hide_badge()
        self.skipped_text.setVisible(False)
        self.show_status("working", "Building the film.")
        self.fast_build.setEnabled(False)
        self.steady_build.setEnabled(False)
        self.fast_build.setToolTip("A build is already running.")
        self.steady_build.setToolTip("A build is already running.")
        self.cancel_button.setVisible(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.step_label.setText("Starting.")
        self._started = time.time()
        self._timer.start()
        self._tick()
        worker = Worker(lambda report, cancel: build_film(
            project, self.state.project_dir, engine,
            report=report, cancel_event=cancel,
            save_cb=self.state.save, force_ram=self._force_ram),
            on_finish=self.state.finish_job)
        self._worker = worker
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_done)
        worker.error.connect(self._on_error)
        worker.start()

    def _tick(self):
        if self._started is None:
            return
        elapsed = int(time.time() - self._started)
        minutes, seconds = divmod(elapsed, 60)
        self.elapsed_label.setText(
            f"Time so far: {minutes}m {seconds:02d}s.")

    def _on_progress(self, label, fraction, detail):
        if fraction is not None:
            self.progress.setValue(int(fraction * 100))
        text = label + (f": {detail}" if detail else "")
        self.step_label.setText(text)
        self.show_status("working", text)
        # The build saves as pieces land: count what is ready now,
        # so the summary moves with the build.
        self._refresh_summary()

    def _finish_ui(self, verb="Took"):
        self._timer.stop()
        if self._started is not None:
            # The build is over: "Time so far" becomes how long it
            # took, and the step label clears instead of lingering
            # on the last step as if the job were still running.
            elapsed = int(time.time() - self._started)
            minutes, seconds = divmod(elapsed, 60)
            self.elapsed_label.setText(
                f"{verb} {minutes}m {seconds:02d}s.")
            self._started = None
        self.step_label.setText("")
        self._worker = None
        self.fast_build.setEnabled(True)
        self.steady_build.setEnabled(True)
        self.cancel_button.setVisible(False)
        self.progress.setVisible(False)
        self._refresh()

    def _on_done(self, result):
        self._film_source = None
        self._finish_ui()
        self.badge.play()
        total = result.get("total_seconds", 0.0)
        self.film_label.setText(
            f"Film: {result.get('film', '')} "
            f"({total:.0f} seconds long)")
        skipped = result.get("skipped") or []
        if skipped:
            self.skipped_text.setPlainText("\n".join(skipped))
            self.skipped_text.setVisible(True)
        self.show_status("done", "Film ready.")

    def _on_error(self, exc):
        self._finish_ui("Stopped after")
        if isinstance(exc, CancelledError):
            self.show_status("idle", "Stopped. Work made so far is "
                             "kept.")
            return
        self.show_plain_error(exc)

    def _cancel_job(self):
        if self._worker is not None:
            self._worker.cancel()
            self.show_status("working", "Stopping.")

    def _open_film(self):
        if self.state.project_dir is None:
            return
        film = self.state.project_dir / scenes_mod.FILM_FILE
        if film.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(film)))

    def _open_folder(self):
        if self.state.project_dir is not None:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self.state.project_dir)))


def run_app(argv):
    app = QApplication(argv)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationName(APP_NAME)
    apply_style(app)
    set_motion_enabled(
        settings().value("motion/enabled", 1, type=int) == 1)
    debuglog.setup_debug_log(paths.log_dir())
    paths.ensure_dirs()

    state = AppState()
    main = MainWindow(state)

    def _raise():
        main.show()
        main.raise_()
        main.activateWindow()

    single = SingleInstance(paths.data_dir() / "hicksfield.lock", _raise)
    if not single.try_acquire():
        return 0
    main.show()
    code = app.exec()
    single.release()
    return code


if __name__ == "__main__":
    sys.exit(run_app(sys.argv))
