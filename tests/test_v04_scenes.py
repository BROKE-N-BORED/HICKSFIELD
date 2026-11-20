"""v0.4 GROUP B tests: task list 3 items 5, 7, 8, 11, 14, 39, 40, 46.

Scenes and Undo: the undo stack is per-film and cleared on a film
switch, every mutation (add, move, reorder, breakdown replace)
pushes an undo snapshot, unsaved words survive a film switch, the
idea echo follows a retyped idea, rows show the camera move, empty
lists say what to do next, and Undo is offered only when it can act.
"""

import unittest
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QMessageBox

from hicksfield import scenes as scenes_mod
from hicksfield.gui import ScenesWindow, StillsWindow
from tests.test_gui import GuiBase


def _row_texts(window, row):
    frame = window.scene_list.itemWidget(window.scene_list.item(row))
    return " ".join(child.text()
                    for child in frame.findChildren(QLabel))


class Item5CrossFilmUndoTests(GuiBase):
    def test_undo_stack_cleared_on_film_switch(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="AAA ONE", narration="One."),
            scenes_mod.Scene(prompt="AAA TWO", narration="Two."),
        ]
        self.state.project_changed.emit()
        window = self._show(ScenesWindow)
        window.scene_list.setCurrentRow(0)
        window.prompt_edit.setPlainText("AAA ONE changed")
        window._save_scene()
        self.assertTrue(window._undo_stack)
        self.assertTrue(window.undo_button.isEnabled())
        self.state.new_project("film b")
        self.assertEqual(window._undo_stack, [])
        self.assertFalse(window.undo_button.isEnabled())
        # Undo in film B must not restore film A's scenes.
        window._undo()
        self.assertEqual(self.state.project.scenes, [])

    def test_undo_stack_cleared_on_opening_another_film(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="AAA ONE", narration="One.")]
        self.state.project_changed.emit()
        window = self._show(ScenesWindow)
        window.scene_list.setCurrentRow(0)
        window.prompt_edit.setPlainText("AAA ONE changed")
        window._save_scene()
        self.assertTrue(window._undo_stack)
        other = self.state.new_project("film b")
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="BBB ONE", narration="One.")]
        self.state.save()
        self.state.open_project(other)
        self.assertEqual(window._undo_stack, [])
        self.assertFalse(window.undo_button.isEnabled())
        window._undo()
        self.assertEqual(
            [s.prompt for s in self.state.project.scenes],
            ["BBB ONE"])


class Item7UndoMutationTests(GuiBase):
    def _two_scenes(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="a stool",
                             narration="A stool sits here."),
            scenes_mod.Scene(prompt="a hammer",
                             narration="A hammer waits."),
        ]
        self.state.project_changed.emit()

    def test_undo_add_removes_only_the_added_scene(self):
        self._two_scenes()
        window = self._show(ScenesWindow)
        window._add_scene()
        self.assertEqual(len(self.state.project.scenes), 3)
        self.assertTrue(window.undo_button.isEnabled())
        window._undo()
        self.assertEqual(
            [s.prompt for s in self.state.project.scenes],
            ["a stool", "a hammer"])

    def test_undo_after_add_keeps_the_edit(self):
        # The B15 shape: edit, then add, then Undo. Without a
        # snapshot for the add, Undo restores the pre-edit list and
        # the added scene vanishes with the edit.
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="a stool",
                             narration="A stool sits here.")]
        self.state.project_changed.emit()
        window = self._show(ScenesWindow)
        window.scene_list.setCurrentRow(0)
        window.prompt_edit.setPlainText("a changed stool")
        window._save_scene()
        window._add_scene()
        self.assertEqual(len(self.state.project.scenes), 2)
        window._undo()
        self.assertEqual(len(self.state.project.scenes), 1)
        self.assertEqual(self.state.project.scenes[0].prompt,
                         "a changed stool")
        window._undo()
        self.assertEqual(self.state.project.scenes[0].prompt,
                         "a stool")

    def test_undo_move_restores_order(self):
        self._two_scenes()
        window = self._show(ScenesWindow)
        window.scene_list.setCurrentRow(0)
        window._move_scene(1)
        self.assertEqual(
            [s.prompt for s in self.state.project.scenes],
            ["a hammer", "a stool"])
        window._undo()
        self.assertEqual(
            [s.prompt for s in self.state.project.scenes],
            ["a stool", "a hammer"])

    def test_undo_drag_reorder_restores_order(self):
        self._two_scenes()
        window = self._show(ScenesWindow)
        item = window.scene_list.takeItem(0)
        window.scene_list.insertItem(1, item)
        window._sync_order_from_list()
        self.assertEqual(
            [s.prompt for s in self.state.project.scenes],
            ["a hammer", "a stool"])
        self.assertTrue(window.undo_button.isEnabled())
        window._undo()
        self.assertEqual(
            [s.prompt for s in self.state.project.scenes],
            ["a stool", "a hammer"])


class Item8AutosaveOnFilmSwitchTests(GuiBase):
    def test_dirty_words_saved_to_old_film_on_switch(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="a stool",
                             narration="A stool sits here.")]
        self.state.project_changed.emit()
        film_a_dir = self.state.project_dir
        self.state.save()
        window = self._show(ScenesWindow)
        window.scene_list.setCurrentRow(0)
        window.prompt_edit.setPlainText("DIRTY UNSAVED WORDS")
        self.state.new_project("film b")
        # Film B is empty and untouched.
        self.assertEqual(self.state.project.scenes, [])
        # Film A's disk file holds the typed words.
        saved = scenes_mod.load_project(film_a_dir)
        self.assertEqual(saved.scenes[0].prompt,
                         "DIRTY UNSAVED WORDS")
        # And the words are not in film B's boxes either.
        self.assertEqual(window.prompt_edit.toPlainText(), "")

    def test_unchanged_boxes_do_not_touch_old_film(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="a stool",
                             narration="A stool sits here.")]
        self.state.project_changed.emit()
        film_a_dir = self.state.project_dir
        self.state.save()
        before = (film_a_dir / "project.json").read_bytes()
        window = self._show(ScenesWindow)
        window.scene_list.setCurrentRow(0)
        self.state.new_project("film b")
        after = (film_a_dir / "project.json").read_bytes()
        self.assertEqual(before, after)


class Item11BreakdownUndoTests(GuiBase):
    def test_breakdown_replace_can_be_undone(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="hand typed",
                             narration="Typed by hand.")]
        self.state.project_changed.emit()
        window = self._show(ScenesWindow)
        window._breakdown_running = True
        window._incoming = [
            scenes_mod.Scene(prompt="model scene one",
                             narration="One."),
            scenes_mod.Scene(prompt="model scene two",
                             narration="Two."),
        ]
        window._commit_incoming()
        self.assertEqual(
            [s.prompt for s in self.state.project.scenes],
            ["model scene one", "model scene two"])
        self.assertTrue(window.undo_button.isEnabled())
        window._undo()
        self.assertEqual(
            [s.prompt for s in self.state.project.scenes],
            ["hand typed"])
        self.assertEqual(self.state.project.scenes[0].narration,
                         "Typed by hand.")

    def test_empty_breakdown_pushes_no_undo(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="hand typed",
                             narration="Typed by hand.")]
        self.state.project_changed.emit()
        window = self._show(ScenesWindow)
        window._breakdown_running = True
        window._incoming = []
        window._commit_incoming()
        self.assertEqual(window._undo_stack, [])
        self.assertFalse(window.undo_button.isEnabled())


class Item14IdeaEchoTests(GuiBase):
    def test_save_emits_project_changed(self):
        seen = []
        self.state.project_changed.connect(lambda: seen.append(1))
        self.state.project.idea = "A stool in a workshop."
        self.state.save()
        self.assertEqual(len(seen), 1)

    def test_save_without_a_project_emits_nothing(self):
        from hicksfield.gui import AppState
        state = AppState()
        seen = []
        state.project_changed.connect(lambda: seen.append(1))
        state.save()
        self.assertEqual(seen, [])

    def test_echo_follows_retyped_idea(self):
        # The user path is MainWindow._save_idea, which sets the idea
        # and calls state.save(); the echo must follow the save.
        self.state.project.idea = "Film A idea."
        self.state.project_changed.emit()
        window = self._show(ScenesWindow)
        self.assertIn("Film A idea.", window.idea_echo.text())
        self.state.project.idea = "A stool in a workshop."
        self.state.save()
        self.assertIn("A stool in a workshop.",
                      window.idea_echo.text())


class Item39CameraMoveInRowTests(GuiBase):
    def test_rows_show_the_camera_move(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="a", narration="b",
                             motion="zoomin"),
            scenes_mod.Scene(prompt="c", narration="d",
                             motion="panleft"),
            scenes_mod.Scene(prompt="e", narration="f", motion=""),
        ]
        self.state.project_changed.emit()
        window = self._show(ScenesWindow)
        self.assertIn("Push in", _row_texts(window, 0))
        self.assertIn("Pan left", _row_texts(window, 1))
        self.assertIn("Auto move", _row_texts(window, 2))

    def test_row_still_shows_ready_marks(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="a stool", narration="A stool.")]
        self.state.project_changed.emit()
        window = self._show(ScenesWindow)
        joined = _row_texts(window, 0)
        self.assertIn("Scene 1: a stool", joined)
        self.assertIn("No picture yet", joined)
        self.assertIn("No voice yet", joined)


class Item40EmptyHintTests(GuiBase):
    def test_scenes_list_shows_hint_when_empty(self):
        window = self._show(ScenesWindow)
        self.assertEqual(window.scene_list.count(), 1)
        item = window.scene_list.item(0)
        self.assertEqual(item.flags(), Qt.NoItemFlags)
        self.assertIn("No scenes yet", _row_texts(window, 0))

    def test_scenes_hint_goes_when_a_scene_arrives(self):
        window = self._show(ScenesWindow)
        self.assertIn("No scenes yet", _row_texts(window, 0))
        window._add_scene()
        self.assertEqual(window.scene_list.count(), 1)
        item = window.scene_list.item(0)
        self.assertNotEqual(item.flags(), Qt.NoItemFlags)
        self.assertNotIn("No scenes yet", _row_texts(window, 0))

    def test_scenes_hint_returns_when_last_scene_removed(self):
        window = self._show(ScenesWindow)
        window._add_scene()
        with mock.patch.object(
                QMessageBox, "question",
                return_value=QMessageBox.Yes):
            window._remove_scene()
        self.assertEqual(window.scene_list.count(), 1)
        self.assertEqual(window.scene_list.item(0).flags(),
                         Qt.NoItemFlags)
        self.assertIn("No scenes yet", _row_texts(window, 0))

    def test_stills_list_shows_hint_when_empty(self):
        window = self._show(StillsWindow)
        self.assertEqual(window.still_list.count(), 1)
        item = window.still_list.item(0)
        self.assertEqual(item.flags(), Qt.NoItemFlags)
        self.assertIn("No scenes yet", item.text())

    def test_stills_hint_goes_when_a_scene_arrives(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="a", narration="b")]
        self.state.project_changed.emit()
        window = self._show(StillsWindow)
        self.assertEqual(window.still_list.count(), 1)
        self.assertIn("no picture yet",
                      window.still_list.item(0).text())


class Item46UndoButtonStateTests(GuiBase):
    def test_undo_disabled_during_breakdown(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="a stool",
                             narration="A stool sits here.")]
        self.state.project_changed.emit()
        window = self._show(ScenesWindow)
        window.scene_list.setCurrentRow(0)
        window.prompt_edit.setPlainText("a changed stool")
        window._save_scene()
        self.assertTrue(window.undo_button.isEnabled())
        window._breakdown_running = True
        window._set_breakdown_ui(True)
        self.assertFalse(window.undo_button.isEnabled())
        window._breakdown_running = False
        window._set_breakdown_ui(False)
        self.assertTrue(window.undo_button.isEnabled())

    def test_undo_disabled_with_empty_stack_after_refresh(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="a stool",
                             narration="A stool sits here.")]
        self.state.project_changed.emit()
        window = self._show(ScenesWindow)
        self.assertFalse(window.undo_button.isEnabled())
        window._refresh()
        self.assertFalse(window.undo_button.isEnabled())
        QTest.qWait(50)
        self.assertFalse(window.undo_button.isEnabled())


class List6AddSceneTests(GuiBase):
    """List 6 item 4: words typed with no scene chosen become
    the new scene when Add is pressed, instead of being cleared
    away; words typed over a chosen scene stay that scene's
    edits."""

    def test_add_starts_from_typed_words(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="a stool",
                             narration="A stool sits here.")]
        self.state.project_changed.emit()
        window = self._show(ScenesWindow)
        # Nothing chosen: the boxes hold no scene's words.
        window.scene_list.setCurrentRow(-1)
        self.assertEqual(window._loaded_row, -1)
        window.prompt_edit.setPlainText("a red door")
        window.narration_edit.setPlainText("The door is red.")
        window._add_scene()
        self.assertEqual(len(self.state.project.scenes), 2)
        added = self.state.project.scenes[1]
        self.assertEqual(added.prompt, "a red door")
        self.assertEqual(added.narration, "The door is red.")
        on_disk = scenes_mod.load_project(self.state.project_dir)
        self.assertEqual(on_disk.scenes[1].prompt, "a red door")
        self.assertIn("made from the words you typed",
                      window.status_label.text())

    def test_add_with_a_chosen_scene_keeps_its_edits_there(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="a stool",
                             narration="A stool sits here.")]
        self.state.project_changed.emit()
        window = self._show(ScenesWindow)
        window.scene_list.setCurrentRow(0)
        window.prompt_edit.setPlainText("a changed stool")
        window._add_scene()
        # The edit stayed with the chosen scene (the autosave on
        # the row switch), and the new scene starts blank.
        self.assertEqual(self.state.project.scenes[0].prompt,
                         "a changed stool")
        self.assertEqual(self.state.project.scenes[1].prompt, "")


if __name__ == "__main__":
    unittest.main()
