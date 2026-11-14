import unittest
from unittest import mock

from tests.helpers import TempEnvTestCase, app, require_qt


class StyleTests(TempEnvTestCase):
    def setUp(self):
        super().setUp()
        require_qt(self)
        self.app = app()

    def test_stylesheet_has_no_em_dash_or_ampersand(self):
        from hicksfield.style import HICKSFIELD_STYLESHEET
        self.assertNotIn("\u2014", HICKSFIELD_STYLESHEET)
        self.assertNotIn("&", HICKSFIELD_STYLESHEET)

    def test_stylesheet_applies(self):
        from hicksfield.style import HICKSFIELD_STYLESHEET, apply_style
        apply_style(self.app)
        self.assertEqual(self.app.styleSheet(), HICKSFIELD_STYLESHEET)

    def test_curtain_role_has_disabled_state(self):
        from hicksfield.style import CURTAIN, HICKSFIELD_STYLESHEET
        marker = 'QPushButton[role="curtain"]:disabled'
        self.assertIn(marker, HICKSFIELD_STYLESHEET)
        body = HICKSFIELD_STYLESHEET.split(marker)[1].split("}")[0]
        self.assertNotIn(CURTAIN, body)

    def test_lamp_states(self):
        from hicksfield.style import StatusLamp
        lamp = StatusLamp()
        self.assertEqual(lamp.state, "idle")
        for state in ("working", "done", "error"):
            lamp.set_state(state)
            self.assertEqual(lamp.state, state)
        lamp.set_state("nonsense")
        self.assertEqual(lamp.state, "idle")

    def test_filmstrip_orientations(self):
        from PySide6.QtCore import Qt
        from hicksfield.style import FilmStrip
        horizontal = FilmStrip(Qt.Horizontal)
        self.assertEqual(horizontal.height(), 18)
        vertical = FilmStrip(Qt.Vertical)
        self.assertEqual(vertical.width(), 18)

    def test_dip_button(self):
        from hicksfield.style import DipButton
        button = DipButton("Press me")
        button.show()
        self.assertFalse(button.property("pressed"))
        button.pressed.emit()
        self.assertTrue(button.property("pressed"))
        button.released.emit()
        self.assertFalse(button.property("pressed"))

    def test_finish_badge_reduced_motion(self):
        from hicksfield import style
        from hicksfield.style import FinishBadge
        badge = FinishBadge()
        self.assertFalse(badge.isVisible())
        with mock.patch.object(style, "REDUCED_MOTION", True):
            badge.play()
        self.assertTrue(badge.isVisible())
        self.assertAlmostEqual(badge._effect.opacity(), 1.0)
        badge.hide_badge()
        self.assertFalse(badge.isVisible())

    def test_finish_badge_animates(self):
        from PySide6.QtTest import QTest
        from hicksfield import style
        from hicksfield.style import FinishBadge
        badge = FinishBadge()
        with mock.patch.object(style, "REDUCED_MOTION", False):
            badge.play()
            QTest.qWait(300)
        self.assertTrue(badge.isVisible())
        self.assertAlmostEqual(badge._effect.opacity(), 1.0, delta=0.05)


if __name__ == "__main__":
    unittest.main()
