"""The HicksField look: a small midnight cinema.

Dark room, warm projector light. Brass for the things you press, deep
theater red for the identity, cream for the words. Film frames hold the
scenes; a film strip runs under the header of every window.

Motion is cheap and short: buttons answer when pressed (a stylesheet
dip, no layout change), the finish moment fades in once over 200 ms
(opacity only). Nothing loops, nothing runs at rest. Set
HICKSFIELD_REDUCED_MOTION=1 to skip the finish fade entirely.
"""

import os

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    Qt,
)
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect,
    QLabel,
    QPushButton,
    QWidget,
)

# Palette: Midnight Cinema.
BG = "#171210"
PANEL = "#221b17"
PANEL_LIGHT = "#2b211b"
BORDER = "#3d3129"
BORDER_LIGHT = "#54443a"
TEXT = "#f2e9d8"
MUTED = "#b3a48e"
BRASS = "#d9a441"
BRASS_HOVER = "#e8b95c"
BRASS_DARK = "#a87c2a"
BRASS_TEXT = "#1d150a"
CURTAIN = "#8e2f32"
CURTAIN_DARK = "#6e2326"
CURTAIN_LIGHT = "#b04044"
GREEN = "#7fb069"
GREY = "#6b6156"
ERROR = "#c05a4d"

REDUCED_MOTION = os.environ.get("HICKSFIELD_REDUCED_MOTION") == "1"

# Motion off from the setting in How to Use (item 20): the finish fade
# is skipped when this is set, the same as the environment variable.
# Read at startup from QSettings and changed from the checkbox.
_motion_off = False


def set_motion_enabled(enabled):
    global _motion_off
    _motion_off = not enabled


def motion_enabled():
    return not REDUCED_MOTION and not _motion_off

FINISH_FADE_MS = 200
DIP_PRESS_MS = 150

HICKSFIELD_STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-size: 12px;
}}
QLabel[role="title"] {{
    font-size: 22px;
    font-weight: bold;
    color: {TEXT};
}}
QLabel[role="subtitle"] {{
    font-size: 12px;
    color: {MUTED};
}}
QLabel[role="muted"] {{
    color: {MUTED};
}}
QLabel[role="section"] {{
    font-size: 13px;
    font-weight: bold;
    color: {BRASS};
}}
QLabel[role="error"] {{
    color: {ERROR};
}}
QLabel[role="ok"] {{
    color: {GREEN};
}}
QFrame[role="card"] {{
    background-color: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QFrame[role="scene"] {{
    background-color: {PANEL};
    border: 1px solid {BORDER};
    border-left: 4px solid {CURTAIN};
    border-radius: 8px;
}}
QFrame[role="scene-done"] {{
    background-color: {PANEL};
    border: 1px solid {BORDER};
    border-left: 4px solid {GREEN};
    border-radius: 8px;
}}
QPushButton {{
    background-color: {PANEL_LIGHT};
    color: {TEXT};
    border: 1px solid {BORDER_LIGHT};
    border-radius: 6px;
    padding: 7px 14px;
}}
QPushButton:hover {{
    background-color: #332820;
    border-color: {BRASS_DARK};
}}
QPushButton:pressed {{
    background-color: #1a130f;
    border-color: {BORDER};
}}
QPushButton:disabled {{
    color: {GREY};
    background-color: {PANEL};
    border-color: {BORDER};
}}
QPushButton[role="small"] {{
    padding: 3px 10px;
}}
QPushButton:focus {{
    border: 1px solid {BRASS};
}}
QPushButton[role="primary"]:focus {{
    border: 1px solid {BRASS_TEXT};
}}
QPushButton[role="curtain"]:focus {{
    border: 1px solid {BRASS};
}}
QPushButton[role="tile"] {{
    background-color: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 16px;
    padding: 12px;
    text-align: left;
}}
QPushButton[role="tile"]:hover {{
    background-color: {PANEL_LIGHT};
    border-color: {BRASS_DARK};
}}
QPushButton[role="tile"]:pressed {{
    background-color: #1a130f;
}}
QPushButton[role="tile"]:focus {{
    border: 1px solid {BRASS};
}}
QLabel[role="tile-number"] {{
    font-size: 28px;
    font-weight: bold;
    color: {TEXT};
}}
QLabel[role="tile-label"] {{
    font-size: 12px;
    color: {MUTED};
}}
QLabel[role="badge"] {{
    color: {BRASS_TEXT};
    background-color: {BRASS};
    border-radius: 4px;
    padding: 2px 6px;
    font-weight: bold;
}}
QPushButton[role="primary"] {{
    background-color: {BRASS};
    color: {BRASS_TEXT};
    border: 1px solid {BRASS_DARK};
    font-weight: bold;
}}
QPushButton[role="primary"]:hover {{
    background-color: {BRASS_HOVER};
}}
QPushButton[role="primary"]:pressed {{
    background-color: {BRASS_DARK};
    color: {TEXT};
}}
QPushButton[role="primary"]:disabled {{
    background-color: {PANEL_LIGHT};
    color: {GREY};
    border-color: {BORDER};
}}
QPushButton[role="curtain"] {{
    background-color: {CURTAIN};
    color: {TEXT};
    border: 1px solid {CURTAIN_DARK};
    font-weight: bold;
}}
QPushButton[role="curtain"]:hover {{
    background-color: {CURTAIN_LIGHT};
}}
QPushButton[role="curtain"]:pressed {{
    background-color: {CURTAIN_DARK};
}}
QPushButton[role="curtain"]:disabled {{
    background-color: {PANEL_LIGHT};
    color: {GREY};
    border-color: {BORDER};
}}
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox {{
    background-color: #100d0b;
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px;
    selection-background-color: {CURTAIN};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{
    border: 1px solid {BRASS};
}}
QComboBox::drop-down {{
    border: none;
}}
QListWidget {{
    background-color: #100d0b;
    border: 1px solid {BORDER};
    border-radius: 6px;
    outline: none;
}}
QListWidget::item {{
    padding: 8px;
    border-bottom: 1px solid {BORDER};
}}
QListWidget::item:selected {{
    background-color: {CURTAIN_DARK};
    color: {TEXT};
}}
QProgressBar {{
    background-color: #100d0b;
    border: 1px solid {BORDER};
    border-radius: 6px;
    height: 18px;
    text-align: center;
    color: {TEXT};
}}
QProgressBar::chunk {{
    background-color: {BRASS};
    border-radius: 5px;
}}
QMenuBar {{
    background-color: {PANEL};
    color: {TEXT};
    border-bottom: 1px solid {BORDER};
}}
QMenuBar::item:selected {{
    background-color: {CURTAIN_DARK};
}}
QMenu {{
    background-color: {PANEL};
    color: {TEXT};
    border: 1px solid {BORDER};
}}
QMenu::item:selected {{
    background-color: {CURTAIN_DARK};
}}
QScrollBar:vertical {{
    background-color: {PANEL};
    width: 12px;
}}
QScrollBar::handle:vertical {{
    background-color: {BORDER_LIGHT};
    border-radius: 6px;
    min-height: 24px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 8px;
    margin-top: 12px;
    padding-top: 10px;
    font-weight: bold;
    color: {BRASS};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}}
QSplitter::handle {{
    background-color: {BORDER};
}}
QToolTip {{
    background-color: {PANEL_LIGHT};
    color: {TEXT};
    border: 1px solid {BORDER_LIGHT};
}}
"""


def apply_style(app):
    app.setStyleSheet(HICKSFIELD_STYLESHEET)


class FilmStrip(QWidget):
    """A strip of film: dark band with sprocket holes.

    Horizontal by default; vertical when orientation is Qt.Vertical.
    Static paint, no animation, cheap to draw.
    """

    def __init__(self, orientation=Qt.Horizontal, parent=None):
        super().__init__(parent)
        self._orientation = orientation
        if orientation == Qt.Horizontal:
            self.setFixedHeight(18)
        else:
            self.setFixedWidth(18)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        painter.setBrush(QColor(PANEL_LIGHT))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(QRectF(rect), 4, 4)
        hole_color = QColor(BG)
        painter.setBrush(hole_color)
        if self._orientation == Qt.Horizontal:
            y_top, y_bottom = 3, rect.height() - 8
            x = 6
            while x + 8 <= rect.width() - 4:
                painter.drawRoundedRect(QRectF(x, y_top, 8, 5), 1, 1)
                painter.drawRoundedRect(QRectF(x, y_bottom, 8, 5), 1, 1)
                x += 14
        else:
            x_left, x_right = 3, rect.width() - 8
            y = 6
            while y + 8 <= rect.height() - 4:
                painter.drawRoundedRect(QRectF(x_left, y, 5, 8), 1, 1)
                painter.drawRoundedRect(QRectF(x_right, y, 5, 8), 1, 1)
                y += 14


LAMP_COLORS = {
    "idle": GREY,
    "working": BRASS,
    "done": GREEN,
    "error": ERROR,
}


class StatusLamp(QWidget):
    """The projector lamp: a small light that says what the app is doing.

    Grey idle, brass working, green done, red error. Calm: it changes
    state, it never blinks.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = "idle"
        self.setFixedSize(14, 14)

    @property
    def state(self):
        return self._state

    def set_state(self, state):
        if state not in LAMP_COLORS:
            state = "idle"
        self._state = state
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = QColor(LAMP_COLORS[self._state])
        painter.setBrush(color)
        painter.setPen(QPen(QColor(BORDER_LIGHT), 1))
        painter.drawEllipse(1, 1, 12, 12)


class DipButton(QPushButton):
    """A button that answers when pressed.

    Pressing swaps in a darker, inset look for the press; releasing
    restores it. Stylesheet only, no geometry change, so the layout
    never moves.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setProperty("pressed", False)
        self.pressed.connect(self._on_pressed)
        self.released.connect(self._on_released)

    def _repolish(self):
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def _on_pressed(self):
        self.setProperty("pressed", True)
        self._repolish()

    def _on_released(self):
        self.setProperty("pressed", False)
        self._repolish()


class FinishBadge(QLabel):
    """The finish moment: a small checkmark that fades in once, 200 ms,
    opacity only. Shown when a film finishes building."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("role", "ok")
        self.setText("\u2713  Film ready")
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)
        self._animation = QPropertyAnimation(self._effect, b"opacity", self)
        self._animation.setDuration(FINISH_FADE_MS)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)
        self.setVisible(False)

    def play(self):
        self.setVisible(True)
        if not motion_enabled():
            self._effect.setOpacity(1.0)
            return
        self._animation.stop()
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.start()

    def hide_badge(self):
        self._animation.stop()
        self._effect.setOpacity(0.0)
        self.setVisible(False)
