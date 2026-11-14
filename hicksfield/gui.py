"""The windows: one function, one window.

Main window is the hub: the idea and the way in. Scenes, Stills, Voice
and Export each have their own window. Long work runs in worker threads;
the window that started it shows real progress and a cancel button.
Only one heavy job runs at a time across the whole app.
"""

import json
import logging
import re
import sys
import time
import weakref
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QSettings, QTimer, QUrl
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
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    HAVE_MULTIMEDIA = True
except ImportError:
    HAVE_MULTIMEDIA = False
    QAudioOutput = None
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
    video,
    voice,
)
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


def slugify(name):
    text = re.sub(r"[^a-z0-9]+", "-", name.strip().lower())
    return text.strip("-") or "film"


class AppState(QObject):
    """The shared state: the current film and the one-job lock."""

    project_changed = Signal()
    job_changed = Signal(object)

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
        self.project = scenes_mod.Project(idea="", scenes=[])
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
        lands; the state always saves its own project."""
        if self.project_dir is None:
            return
        scenes_mod.save_project(self.project, self.project_dir)

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
        self.text.setPlainText(debuglog.build_debug_report(
            __version__, machine.machine_summary()))
        self.layout.addWidget(self.text, 1)
        row = QHBoxLayout()
        copy_button = DipButton("Copy the report")
        copy_button.clicked.connect(self._copy)
        close_button = DipButton("Close")
        close_button.clicked.connect(self.close)
        row.addStretch(1)
        row.addWidget(copy_button)
        row.addWidget(close_button)
        self.layout.addLayout(row)
        self.finish_layout()

    def _copy(self):
        QApplication.clipboard().setText(self.text.toPlainText())
        self.show_status("done", "Copied.")


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
            "Open the Scenes window and break the idea into scenes.")
        self.scenes_button.clicked.connect(
            lambda: self.open_window("scenes"))
        buttons.addWidget(self.scenes_button)
        self.stills_button = DipButton("Stills")
        self.stills_button.setToolTip("Open the Stills window.")
        self.stills_button.clicked.connect(
            lambda: self.open_window("stills"))
        buttons.addWidget(self.stills_button)
        self.voice_button = DipButton("Voice")
        self.voice_button.setToolTip("Open the Voice window.")
        self.voice_button.clicked.connect(
            lambda: self.open_window("voice"))
        buttons.addWidget(self.voice_button)
        self.export_button = DipButton("Export")
        self.export_button.setProperty("role", "curtain")
        self.export_button.setToolTip(
            "Open the Export window and build the film.")
        self.export_button.clicked.connect(
            lambda: self.open_window("export"))
        buttons.addWidget(self.export_button)
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
        QWidget.setTabOrder(self.scenes_button, self.stills_button)
        QWidget.setTabOrder(self.stills_button, self.voice_button)
        QWidget.setTabOrder(self.voice_button, self.export_button)

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
            ("ffmpeg", None, None),
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
        voice_ready, _s = voice.is_ready()
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
            else "ffmpeg is missing. Install it with: "
            "sudo apt install ffmpeg")
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
        worker = Worker(lambda report, cancel: ollama.is_reachable(3))
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
        if project_dir is not None:
            for scene in project.scenes:
                for field, counter in (("still", "stills_ready"),
                                       ("audio", "voice_ready"),
                                       ("clip", "clips_ready")):
                    value = getattr(scene, field)
                    if value and (project_dir / value).exists():
                        if counter == "stills_ready":
                            stills_ready += 1
                        elif counter == "voice_ready":
                            voice_ready += 1
                        else:
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
        self.machine_label.setText(
            f"Free memory: about "
            f"{_gb_text(machine.mem_available_mb())}. Free disk: about "
            f"{_gb_text(machine.disk_free_mb(str(Path.home())))}.")
        self._refresh_checklist()
        self._refresh_recent_menu()

    def _refresh_recent_menu(self):
        self._recent_menu.clear()
        recent = settings().value("recent", []) or []
        if not recent:
            action = QAction("No recent films", self)
            action.setEnabled(False)
            self._recent_menu.addAction(action)
            return
        for path in recent:
            action = QAction(Path(path).name, self)
            action.triggered.connect(
                lambda _c, p=path: self._open_recent(p))
            self._recent_menu.addAction(action)

    def _remember_recent(self, project_dir):
        recent = settings().value("recent", []) or []
        text = str(project_dir)
        if text in recent:
            recent.remove(text)
        recent.insert(0, text)
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
            self.lamp.set_state("idle")
            self.status_label.setText("Ready.")
        else:
            self.lamp.set_state("working")
            self.status_label.setText(f"Working: {owner}.")

    def _about(self):
        QMessageBox.about(
            self, "About HicksField",
            "HicksField 0.2.0\n\nA free local film studio by "
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
        self._breakdown_running = False
        self._incoming = []
        self.idea_echo = QLabel()
        self.idea_echo.setProperty("role", "muted")
        self.idea_echo.setWordWrap(True)
        self.layout.addWidget(self.idea_echo)

        row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setPlaceholderText("Pick a model")
        self.model_combo.setToolTip(
            "Pick the model that breaks your idea into scenes.")
        row.addWidget(self.model_combo, 1)
        self.refresh_button = DipButton("Refresh models")
        self.refresh_button.setToolTip(
            "Look for Ollama and its models again.")
        self.refresh_button.clicked.connect(self._refresh_models)
        row.addWidget(self.refresh_button)
        break_button = DipButton("Break into scenes")
        break_button.setProperty("role", "primary")
        break_button.setToolTip(
            "Ask the model to break the idea into scenes.")
        break_button.clicked.connect(self._break_into_scenes)
        row.addWidget(break_button)
        self.break_button = break_button
        self.layout.addLayout(row)

        model_note = QLabel(
            "Pick the model that breaks your idea into scenes. "
            "Any small local model works.")
        model_note.setProperty("role", "muted")
        model_note.setWordWrap(True)
        self.layout.addWidget(model_note)

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

        edit_row = QHBoxLayout()
        self.save_button = DipButton("Save this scene")
        self.save_button.clicked.connect(self._save_scene)
        edit_row.addWidget(self.save_button)
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

        QWidget.setTabOrder(self.model_combo, self.refresh_button)
        QWidget.setTabOrder(self.refresh_button, self.break_button)
        QWidget.setTabOrder(self.break_button, self.scene_list)
        QWidget.setTabOrder(self.scene_list, self.prompt_edit)
        QWidget.setTabOrder(self.prompt_edit, self.narration_edit)
        QWidget.setTabOrder(self.narration_edit, self.save_button)
        QWidget.setTabOrder(self.save_button, self.add_button)
        QWidget.setTabOrder(self.add_button, self.remove_button)
        QWidget.setTabOrder(self.remove_button, self.move_up_button)
        QWidget.setTabOrder(self.move_up_button, self.move_down_button)

    def _refresh(self):
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
        if 0 <= row < self.scene_list.count():
            self.scene_list.setCurrentRow(row)
        bar.setValue(scroll)

    def _add_scene_item(self, index, scene):
        """One scene as a film-frame card: thumbnail, prompt, and the
        state marks for picture and voice. Both ready earns the green
        edge."""
        project_dir = self.state.project_dir
        still_ok = bool(
            scene.still and project_dir is not None
            and (project_dir / scene.still).exists())
        audio_ok = bool(
            scene.audio and project_dir is not None
            and (project_dir / scene.audio).exists())
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
        if still_ok:
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
        marks = ("Picture ready" if still_ok else "No picture yet",
                 "Voice ready" if audio_ok else "No voice yet")
        mark_label = QLabel("  \u00b7  ".join(marks))
        mark_label.setProperty("role", "muted")
        mark_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        text_box.addWidget(mark_label)
        row_layout.addLayout(text_box, 1)
        item = QListWidgetItem()
        item.setData(Qt.UserRole, index)
        item.setSizeHint(QSize(0, 64))
        self.scene_list.addItem(item)
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
        self.scene_list.clear()
        for index, scene in enumerate(self._incoming):
            self._add_scene_item(index, scene)
        for _ in range(3):
            self._add_skeleton_item()

    def _load_scene(self, row):
        if self._breakdown_running:
            return
        scenes = self.state.project.scenes
        if 0 <= row < len(scenes):
            self.prompt_edit.setPlainText(scenes[row].prompt)
            self.narration_edit.setPlainText(scenes[row].narration)
        else:
            self.prompt_edit.clear()
            self.narration_edit.clear()
        chosen = 0 <= row < len(scenes)
        self.remove_button.setEnabled(chosen)
        self.move_up_button.setEnabled(chosen and row > 0)
        self.move_down_button.setEnabled(
            chosen and row < len(scenes) - 1)
        # The header carries the guidance when nothing is chosen, so
        # no separate hint row is needed.
        self.edit_label.setText(
            "The chosen scene:" if chosen else
            "The chosen scene: pick a scene above to edit it, "
            "or add one below.")

    def _move_scene(self, step):
        row = self.scene_list.currentRow()
        scenes = self.state.project.scenes
        target = row + step
        if not (0 <= row < len(scenes)):
            return
        if not (0 <= target < len(scenes)):
            return
        scenes[row], scenes[target] = scenes[target], scenes[row]
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
        scene.prompt = self.prompt_edit.toPlainText().strip()
        scene.narration = self.narration_edit.toPlainText().strip()
        self.state.save()
        self.state.project_changed.emit()
        self.show_status("done", f"Scene {row + 1} saved.")

    def _add_scene(self):
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
        del scenes[row]
        self.state.save()
        self.state.project_changed.emit()
        self.show_status("done", f"Scene {row + 1} removed.")

    def _refresh_models(self):
        self.show_status("working", "Checking for Ollama.")
        self.break_button.setEnabled(False)
        worker = Worker(self._check_ollama)
        self._worker = worker
        worker.done.connect(self._on_models)
        worker.error.connect(self._on_models_error)
        worker.start()

    @staticmethod
    def _check_ollama(_report, _cancel):
        return ollama.list_models()

    def _on_models(self, models):
        self._worker = None
        self._ollama_ok = True
        self.model_combo.clear()
        self.model_combo.addItems(models)
        self.model_combo.setCurrentIndex(-1)
        self.break_button.setEnabled(True)
        if models:
            self.show_status(
                "done",
                f"Ollama is running with {plural(len(models), 'model')}. "
                "Pick one, then break the idea into scenes.")
        else:
            self.show_status(
                "idle",
                "Ollama is running but has no models. Pull one first, "
                "or type scenes by hand below.")
        self.ollama_label.setText(
            "Ollama is running." if models else
            "Ollama is running, no models found.")

    def _on_models_error(self, exc):
        self._worker = None
        self._ollama_ok = False
        self.model_combo.clear()
        self.break_button.setEnabled(False)
        self.ollama_label.setText(
            "Ollama is not running. Scenes can be typed by hand below.")
        if isinstance(exc, OllamaUnreachableError):
            self.show_status("idle", exc.plain_text())
        else:
            self.show_plain_error(exc)

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
        self._set_breakdown_ui(True)
        self._refresh()
        self.show_status("working", "Breaking the idea into scenes.")
        worker = Worker(
            lambda report, cancel: self._run_breakdown(
                model, report, cancel),
            on_finish=self.state.finish_job)
        self._worker = worker
        worker.progress.connect(self._on_breakdown_progress)
        worker.done.connect(self._on_breakdown)
        worker.error.connect(self._on_breakdown_error)
        worker.start()

    def _run_breakdown(self, model, report, cancel):
        def _on_scene(scene):
            report("scene", None, json.dumps(scene))

        return ollama.break_into_scenes_streaming(
            self.state.project.idea, model, _on_scene,
            report=report, cancel_event=cancel)

    def _set_breakdown_ui(self, running):
        self.breakdown_progress.setVisible(running)
        if running:
            self.breakdown_progress.setRange(0, 0)
        self.breakdown_cancel.setVisible(running)
        self.break_button.setEnabled(not running)
        self.refresh_button.setEnabled(not running)
        self.model_combo.setEnabled(not running)
        self.scene_list.setEnabled(not running)
        self.prompt_edit.setEnabled(not running)
        self.narration_edit.setEnabled(not running)
        self.save_button.setEnabled(not running)
        self.add_button.setEnabled(not running)
        self.remove_button.setEnabled(not running)
        self.move_up_button.setEnabled(not running)
        self.move_down_button.setEnabled(not running)

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
        self.show_status(
            "working",
            f"Breaking the idea into scenes. {detail}")

    def _commit_incoming(self):
        self._breakdown_running = False
        self._set_breakdown_ui(False)
        if self._incoming:
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
        self.engines = stills.discover_engines()
        self.fast_engine = self.engines[0]
        self.steady_engine = self.engines[1]

        ram = QLabel()
        ram.setProperty("role", "muted")
        ram.setWordWrap(True)
        available = machine.mem_available_mb()
        ram.setText(
            f"Free memory right now: about {available // 1024} GB. "
            "Each engine needs about 3 GB while it makes a picture. "
            "Pictures are made one at a time.")
        self.layout.addWidget(ram)

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
            "Make pictures for the scenes that have none yet.")
        self.make_button.clicked.connect(self._make_missing)
        row.addWidget(self.make_button)
        self.cancel_button = DipButton("Cancel")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self._cancel_job)
        row.addWidget(self.cancel_button)
        row.addStretch(1)
        self.layout.addLayout(row)
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

    def _show_preview(self, row):
        scenes = self.state.project.scenes
        project_dir = self.state.project_dir
        if 0 <= row < len(scenes):
            scene = scenes[row]
            if scene.still and project_dir is not None:
                still_path = project_dir / scene.still
                if still_path.exists():
                    self.preview.set_pixmap(QPixmap(str(still_path)))
                    self.preview_caption.setText(
                        f"Scene {row + 1}: the picture.")
                    return
            self.preview.set_pixmap(None)
            self.preview_caption.setText(
                f"Scene {row + 1}: no picture yet.")
        else:
            self.preview.set_pixmap(None)
            self.preview_caption.setText(
                "Pick a picture to see it large.")

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
        project_dir = self.state.project_dir
        placeholder = _placeholder_icon()
        for index, scene in enumerate(self.state.project.scenes):
            if scene.still and project_dir is not None:
                still_path = project_dir / scene.still
                if still_path.exists():
                    pixmap = QPixmap(str(still_path))
                    icon = QIcon(pixmap.scaled(
                        96, 96, Qt.KeepAspectRatio,
                        Qt.SmoothTransformation))
                    item = QListWidgetItem(
                        icon, f"Scene {index + 1}: picture ready")
                    self.still_list.addItem(item)
                    continue
            item = QListWidgetItem(
                placeholder, f"Scene {index + 1}: no picture yet")
            self.still_list.addItem(item)
        if 0 <= row < self.still_list.count():
            self.still_list.setCurrentRow(row)
        bar.setValue(scroll)
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
        total = len(project.scenes)
        made = 0
        for index, scene in enumerate(project.scenes):
            if cancel.is_set():
                raise CancelledError()
            if scene.still and (project_dir / scene.still).exists():
                report("Making pictures", (index + 1) / total,
                       f"scene {index + 1} already has a picture")
                continue
            name = scenes_mod.scene_still_name(index)
            out = paths_map["stills"] / name
            engine.generate(scene.prompt, out, cancel_event=cancel,
                            force=self._force_ram)
            scene.still = f"{scenes_mod.STILLS_DIR}/{name}"
            made += 1
            self.state.save()
            report("Making pictures", (index + 1) / total,
                   f"scene {index + 1} of {total}")
        return made

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
            "One voice reads the lines. It is small and runs on this "
            "computer.")
        blurb.setProperty("role", "muted")
        blurb.setWordWrap(True)
        card_layout.addWidget(blurb)
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
            "Speak the lines that have none yet.")
        self.make_button.clicked.connect(self._make_lines)
        row.addWidget(self.make_button)
        self.cancel_button = DipButton("Cancel")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self._cancel_job)
        row.addWidget(self.cancel_button)
        row.addStretch(1)
        self.layout.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.layout.addWidget(self.progress)
        self.finish_layout()
        state.project_changed.connect(_weak_slot(self, "_refresh"))
        self._refresh()

        QWidget.setTabOrder(self.setup_button, self.line_list)
        QWidget.setTabOrder(self.line_list, self.make_button)

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
        ready, status = voice.is_ready()
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
            state_text = "spoken" if done else "not spoken yet"
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
        self._mark_playing_button()

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
            report=report, cancel_event=cancel),
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
        ready, _status = voice.is_ready()
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
        total = len(project.scenes)
        made = 0
        for index, scene in enumerate(project.scenes):
            if cancel.is_set():
                raise CancelledError()
            if scene.audio and (project_dir / scene.audio).exists():
                report("Speaking the lines", (index + 1) / total,
                       f"scene {index + 1} already spoken")
                continue
            name = scenes_mod.scene_audio_name(index)
            out = paths_map["audio"] / name
            voice.synthesize(scene.narration, out, cancel_event=cancel)
            scene.audio = f"{scenes_mod.AUDIO_DIR}/{name}"
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
    def __init__(self, state):
        super().__init__(state, "Export", "export", 850, 600)
        self._worker = None
        self._force_ram = False
        self._started = None
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._player = None
        self._audio = None
        self._film_source = None
        self._duration = 0
        if HAVE_MULTIMEDIA:
            self._player = QMediaPlayer(self)
            self._audio = QAudioOutput(self)
            self._player.setAudioOutput(self._audio)
            self._player.durationChanged.connect(
                self._on_duration_changed)
            self._player.positionChanged.connect(
                self._on_position_changed)
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
        self.layout.addWidget(self.ffmpeg_label)

        build_label = QLabel("Build the film:")
        build_label.setProperty("role", "section")
        self.layout.addWidget(build_label)
        self.engine_note = QLabel()
        self.engine_note.setProperty("role", "muted")
        self.engine_note.setWordWrap(True)
        self.layout.addWidget(self.engine_note)
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
        player_layout.addWidget(self.video_widget)
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
            self._player.play()
            self.play_button.setText("Pause")

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
        self.show_status(
            "idle", "The film preview could not start here. "
            "Use Open the film instead.")

    def _refresh(self):
        project = self.state.project
        project_dir = self.state.project_dir
        count = len(project.scenes)
        stills_ready = voice_ready = clips_ready = 0
        if project_dir is not None:
            for scene in project.scenes:
                if scene.still and (project_dir / scene.still).exists():
                    stills_ready += 1
                if scene.audio and (project_dir / scene.audio).exists():
                    voice_ready += 1
                if scene.clip and (project_dir / scene.clip).exists():
                    clips_ready += 1
        self.summary_label.setText(
            f"{plural(count, 'scene')}. "
            f"{plural(stills_ready, 'picture')} ready. "
            f"{plural(voice_ready, 'spoken line')} ready. "
            f"{plural(clips_ready, 'moving clip')} ready.")
        version = video.ffmpeg_version_short()
        if version:
            self.ffmpeg_label.setText(
                f"ffmpeg is ready (version {version})")
        else:
            self.ffmpeg_label.setText(
                "ffmpeg is missing. Install it with: "
                "sudo apt install ffmpeg")
        engines = stills.discover_engines()
        fast_ready, _s = engines[0].is_ready()
        steady_ready, _s = engines[1].is_ready()
        self.fast_build.setEnabled(bool(count) and fast_ready
                                   and version is not None)
        self.steady_build.setEnabled(bool(count) and steady_ready
                                     and version is not None)
        engine, reason = stills.recommended_engine()
        self.engine_note.setText(reason)
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
        else:
            self.film_label.setText("No film built yet.")
            self.watch_button.setEnabled(False)
            self.folder_button.setEnabled(False)
            self.play_button.setEnabled(False)

    def _build(self, engine):
        project = self.state.project
        if not project.scenes:
            self.show_status(
                "idle", "There are no scenes yet. Break the idea into "
                "scenes first.")
            return
        ready, status = engine.is_ready()
        if not ready:
            self.show_status("idle", f"{engine.label} is not ready: "
                             f"{status}")
            return
        voice_ready, _s = voice.is_ready()
        if not voice_ready:
            self.show_status(
                "idle", "Set up the voice first, in the Voice window.")
            return
        if video.find_ffmpeg() is None:
            self.show_status(
                "error",
                "ffmpeg is missing. Install it with: "
                "sudo apt install ffmpeg")
            return
        try:
            engine.check_ram()
            self._force_ram = False
        except RamShortError as exc:
            if not self.ask_yes_no(
                    "Memory is short",
                    f"{exc}. Build anyway? The computer may slow down "
                    "while the film is built."):
                self.show_status("idle", "Left it. Free some memory "
                                 "and try again.")
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

    def _finish_ui(self):
        self._timer.stop()
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
        self._finish_ui()
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
