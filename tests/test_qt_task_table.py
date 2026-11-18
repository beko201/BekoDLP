"""Model/view contracts, sorted selections and task actions against real Qt."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QAbstractTableModel, QItemSelectionModel, QPersistentModelIndex, Qt
from PySide6.QtTest import QAbstractItemModelTester, QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from task_manager import TaskManager, TaskOptions
from ui import file_actions
from ui.main_window import MainWindow
from ui.task_model import PROGRESS_ROLE
from ui.task_table import ProgressDelegate, TaskTable


def snapshot(tid, progress=0, title=None, status="Downloading - 1.0MB/5.0MB - 2.00 Mbps - Time left: 1m 2s"):
    return dict(id=tid, title=title or tid, format="MP4", quality="1080p", status=status,
                progress=progress, url=f"https://example.com/{tid}", _progress_stage="downloading",
                speed_bps=250000, eta_seconds=62)


class QtTaskTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.table = TaskTable()
        self.addCleanup(self.table.close)
        self.table.resize(1100, 400)
        self.table.show()
        self.app.processEvents()

    def proxy_ids(self):
        return [self.table.proxy.index(row, 0).data(Qt.ItemDataRole.UserRole)
                for row in range(self.table.proxy.rowCount())]

    def test_model_contract_and_persistent_indices_survive_updates_and_removal(self):
        self.assertIsInstance(self.table.model, QAbstractTableModel)
        tester = QAbstractItemModelTester(self.table.model,
                                         QAbstractItemModelTester.FailureReportingMode.Fatal)
        for tid in ("T1", "T2", "T3"):
            self.table.update_task(tid, snapshot(tid))
        persistent = QPersistentModelIndex(self.table.model.index(2, 0))
        self.table.remove_task("T1")
        self.table.update_task("T3", snapshot("T3", 81))
        self.assertTrue(persistent.isValid())
        self.assertEqual(persistent.row(), 1)
        self.assertEqual(persistent.data(Qt.ItemDataRole.UserRole), "T3")
        self.assertEqual(self.table.model.index(1, 5).data(), "81%")
        self.table.remove_task("T2")
        self.table.remove_task("T3")
        self.table.remove_task("missing")
        self.assertEqual(self.table.model.rowCount(), 0)
        self.assertFalse(persistent.isValid())
        self.assertTrue(self.table.empty_label.isVisible())
        self.assertIsNotNone(tester)

    def test_numeric_sorting_and_selection_follow_task_id_after_live_reorder(self):
        for tid, progress in (("T10", 100), ("T2", 9), ("T1", 80)):
            self.table.update_task(tid, snapshot(tid, progress))
        self.assertEqual(self.proxy_ids(), ["T1", "T2", "T10"])
        self.table.view.sortByColumn(5, Qt.SortOrder.AscendingOrder)
        self.assertEqual(self.proxy_ids(), ["T2", "T1", "T10"])
        self.table.view.selectRow(0)
        self.table.update_task("T2", snapshot("T2", 99))
        self.assertEqual(self.proxy_ids(), ["T1", "T2", "T10"])
        self.assertEqual(self.table.selected_task_ids(), ["T2"])
        self.table.remove_task("T1")
        self.assertEqual(self.table.selected_task_ids(), ["T2"])

    def test_bulk_insert_mixes_updates_and_duplicates_without_resetting_selection(self):
        tester = QAbstractItemModelTester(self.table.model,
                                         QAbstractItemModelTester.FailureReportingMode.Fatal)
        self.table.update_task("T2", snapshot("T2", 10))
        persistent = QPersistentModelIndex(self.table.model.index(0, 0))
        self.table.view.selectRow(0)
        inserted, resets = [], []
        self.table.model.rowsInserted.connect(lambda parent, first, last: inserted.append((first, last)))
        self.table.model.modelReset.connect(lambda: resets.append(True))
        self.table.update_tasks([("T3", snapshot("T3")), ("T2", snapshot("T2", 75)),
                                 ("T1", snapshot("T1", 20)), ("T1", snapshot("T1", 90))])
        self.assertEqual(inserted, [(1, 2)])
        self.assertEqual(resets, [])
        self.assertEqual(self.proxy_ids(), ["T1", "T2", "T3"])
        self.assertEqual(self.table.selected_task_ids(), ["T2"])
        self.assertTrue(persistent.isValid())
        self.assertEqual(persistent.data(Qt.ItemDataRole.UserRole), "T2")
        self.assertEqual(self.table.model.task_for_id("T2")["progress"], 75)
        self.assertEqual(self.table.model.task_for_id("T1")["progress"], 90)
        self.assertIsNotNone(tester)

    def test_bulk_remove_ranges_preserves_survivor_indices_and_sorted_selection(self):
        tester = QAbstractItemModelTester(self.table.model,
                                         QAbstractItemModelTester.FailureReportingMode.Fatal)
        self.table.update_tasks([(f"T{i}", snapshot(f"T{i}")) for i in range(7)])
        persistent = QPersistentModelIndex(self.table.model.index(4, 0))
        deleted = QPersistentModelIndex(self.table.model.index(3, 0))
        self.table.view.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        self.table.view.selectRow(2)  # T4
        removed, resets = [], []
        self.table.model.rowsRemoved.connect(lambda parent, first, last: removed.append((first, last)))
        self.table.model.modelReset.connect(lambda: resets.append(True))
        self.table.remove_tasks(["T3", "T0", "T5", "T2", "T2", "missing"])
        self.assertEqual(removed, [(5, 5), (2, 3), (0, 0)])
        self.assertEqual(resets, [])
        self.assertEqual(self.proxy_ids(), ["T6", "T4", "T1"])
        self.assertEqual(self.table.selected_task_ids(), ["T4"])
        self.assertTrue(persistent.isValid())
        self.assertEqual(persistent.row(), 1)
        self.assertEqual(persistent.data(Qt.ItemDataRole.UserRole), "T4")
        self.assertFalse(deleted.isValid())
        self.table.update_task("T4", snapshot("T4", 81))
        self.assertEqual(self.table.model.index(1, 5).data(), "81%")
        removed.clear()
        self.table.remove_tasks(["T1", "T4", "T6"])
        self.assertEqual(removed, [(0, 2)])
        self.assertFalse(persistent.isValid())
        self.assertTrue(self.table.empty_label.isVisible())
        self.table.update_tasks([])
        self.table.remove_tasks(["missing"])
        self.assertEqual(self.table.model.rowCount(), 0)
        self.assertIsNotNone(tester)

    def test_speed_eta_and_resolution_sort_numerically_and_stale_values_clear(self):
        a = snapshot("T1", status="Downloading - 10.00 Mbps - Time left: 2m 0s")
        b = snapshot("T2", status="Downloading - 2.00 Mbps - Time left: 59s")
        a.update(speed_bps=1250000, eta_seconds=120)
        b.update(speed_bps=250000, eta_seconds=59)
        a["quality"], b["quality"] = "720p", "1080p"
        self.table.update_task("T1", a)
        self.table.update_task("T2", b)
        for column, expected in ((6, ["T2", "T1"]), (7, ["T2", "T1"]), (3, ["T1", "T2"])):
            self.table.view.sortByColumn(column, Qt.SortOrder.AscendingOrder)
            self.assertEqual(self.proxy_ids(), expected)
        self.assertEqual(self.table.model.index(0, 7).data(), "2m 0s")
        a["status"] = "Completed (MP4)"
        self.table.update_task("T1", a)
        self.assertEqual(self.table.model.index(0, 6).data(), "—")
        self.assertEqual(self.table.model.index(0, 7).data(), "—")

    def test_snapshot_isolation_and_progress_are_accessible_without_cell_widgets(self):
        task = snapshot("T1", 45.5)
        task["warnings"] = ["original"]
        self.table.update_task("T1", task)
        task["warnings"].append("external")
        returned = self.table.model.task_for_id("T1")
        returned["warnings"].append("caller")
        self.assertEqual(self.table.model.task_for_id("T1")["warnings"], ["original"])
        index = self.table.model.index(0, 5)
        self.assertEqual(index.data(PROGRESS_ROLE), 45.5)
        self.assertEqual(index.data(Qt.ItemDataRole.AccessibleTextRole), "46%")
        self.assertIsInstance(self.table.view.itemDelegateForColumn(5), ProgressDelegate)
        self.assertIsNone(self.table.view.indexWidget(self.table.proxy.index(0, 5)))
        self.assertFalse(index.flags() & Qt.ItemFlag.ItemIsEditable)
        self.assertFalse(self.table.grab().isNull())

    def test_progress_only_notifies_its_cell_and_unchanged_snapshots_are_silent(self):
        task = snapshot('T1', 10)
        self.table.update_task('T1', task)
        changes = []
        self.table.model.dataChanged.connect(
            lambda first, last, roles: changes.append((first.column(), last.column(), set(roles))))
        self.table.update_task('T1', dict(task))
        self.assertEqual(changes, [])
        task['progress'] = 10.25  # Painted value changes even if rounded text does not.
        self.table.update_task('T1', task)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0][:2], (5, 5))
        self.assertIn(PROGRESS_ROLE, changes[0][2])
        self.assertNotIn(Qt.ItemDataRole.DisplayRole, changes[0][2])
        self.assertEqual(self.table.model.index(0, 5).data(PROGRESS_ROLE), 10.25)

    def test_table_does_not_retain_engine_options_or_metadata(self):
        task = snapshot('T1', 20)
        task.update(folder='private-folder', trim_start='1', formats=[{'url': 'large metadata'}],
                    _disk_check_at=123, output_path='output.mp4', warnings=['diagnostic'])
        self.table.update_task('T1', task)
        retained = self.table.model.task_for_id('T1')
        self.assertFalse({'folder', 'trim_start', 'formats', '_disk_check_at'} & retained.keys())
        self.assertEqual(retained['output_path'], 'output.mp4')
        self.assertEqual(retained['warnings'], ['diagnostic'])
        self.assertEqual(retained['progress'], 20)

    def test_tooltip_only_changes_and_warning_actions_are_preserved(self):
        task = snapshot('T1', 100, status='Completed')
        task.update(output_mode='video', container='mp4', output_path='first.mp4')
        self.table.update_task('T1', task)
        changes = []
        self.table.model.dataChanged.connect(
            lambda first, last, roles: changes.append((first.column(), last.column(), set(roles))))
        task['url'] = 'https://example.com/replaced'
        self.table.update_task('T1', task)
        self.assertEqual(changes, [(1, 1, {Qt.ItemDataRole.ToolTipRole})])
        self.assertIn('replaced', self.table.model.index(0, 1).data(Qt.ItemDataRole.ToolTipRole))
        changes.clear()
        task['output_path'] = 'second.mp4'
        self.table.update_task('T1', task)
        self.assertEqual(changes, [(2, 2, {Qt.ItemDataRole.ToolTipRole})])
        self.assertIn('second.mp4', self.table.model.index(0, 2).data(Qt.ItemDataRole.ToolTipRole))
        task['warnings'] = ['Verification warning']
        self.table.update_task('T1', task)
        self.assertEqual(self.table.model.index(0, 4).data(), 'Completed with warnings')
        self.assertIn('diagnostic', self.table.model.index(0, 4).data(Qt.ItemDataRole.ToolTipRole))
        self.assertEqual(self.table.model.task_for_id('T1')['warnings'], task['warnings'])

    def test_speed_and_eta_use_numeric_fields_instead_of_status_text(self):
        task = snapshot("T1", status="Downloading - 99.00 Mbps - Time left: 999m 0s")
        task.update(speed_bps=125000, eta_seconds=1.75)
        self.table.update_task("T1", task)
        self.assertEqual(self.table.model.index(0, 6).data(), "1.00 Mbps")
        self.assertEqual(self.table.model.index(0, 7).data(), "1s")
        task.update(speed_bps=float("nan"), eta_seconds=float("inf"))
        self.table.update_task("T1", task)
        self.assertEqual(self.table.model.index(0, 6).data(), "—")
        self.assertEqual(self.table.model.index(0, 7).data(), "--")
        task.update(speed_bps=125000, eta_seconds=1.75, _progress_stage="processing")
        self.table.update_task("T1", task)
        self.assertEqual(self.table.model.index(0, 6).data(), "—")
        self.assertEqual(self.table.model.index(0, 7).data(), "—")

    def test_ctrl_a_selects_table_tasks_without_affecting_url_input(self):
        for tid in ("T1", "T2", "T3"):
            self.table.update_task(tid, snapshot(tid))
        self.table.view.setFocus()
        QTest.keyClick(self.table.view, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(set(self.table.selected_task_ids()), {"T1", "T2", "T3"})

    def test_context_menu_captures_clicked_task_id_and_preserves_multiselection(self):
        for tid in ("T1", "T2"):
            self.table.update_task(tid, snapshot(tid))
        self.table.view.selectAll()
        self.app.processEvents()
        rect = self.table.view.visualRect(self.table.proxy.index(0, 0))
        received = []
        self.table.cancel_requested.connect(received.append)
        self.table._open_context_menu(rect.center())
        self.assertEqual(set(self.table.selected_task_ids()), {"T1", "T2"})
        cancel = next(action for action in self.table._context_menu.actions() if action.text() == "Cancel selected")
        self.table.view.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        cancel.trigger()
        self.assertEqual(set(received[0]), {"T1", "T2"})
        self.table._context_menu.close()


class QtTaskActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.manager = TaskManager(engine_factory=lambda manager: Mock())
        # Leave queued tasks waiting so actions can be tested without worker races.
        with patch.object(self.manager, "set_worker_count"):
            self.window = MainWindow(self.manager, settings_path=self.folder / "settings.json")
        self.window.show()
        self.addCleanup(self.close_window)

    def close_window(self):
        self.window.close()
        self.window.controller.poll()
        self.app.processEvents()

    def add_task(self, status="Queued", title="Task", output=None):
        tid = self.manager.add_urls([f"https://example.com/{len(self.manager.list_tasks())}"],
                                    TaskOptions(folder=str(self.folder)))[0]
        self.manager.update_task(tid, title=title, status=status, output_path=output,
                                 progress=100 if output else 0)
        self.window.controller.poll()
        return tid

    def select(self, *tids):
        table = self.window.task_table
        table.view.clearSelection()
        for row in range(table.proxy.rowCount()):
            index = table.proxy.index(row, 0)
            if index.data(Qt.ItemDataRole.UserRole) in tids:
                table.view.selectionModel().select(index, QItemSelectionModel.SelectionFlag.Select |
                                                   QItemSelectionModel.SelectionFlag.Rows)

    def question(self):
        return next(iter(self.window._message_boxes))

    def test_cancellation_confirmation_keeps_original_ids_when_sort_changes(self):
        first, second, other = self.add_task(), self.add_task(), self.add_task()
        self.select(first, second)
        self.window.cancel_action.trigger()
        box = self.question()
        self.window.task_table.view.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        self.select(other)
        box.done(QMessageBox.StandardButton.Yes)
        self.assertEqual(self.manager.get_task(first)["status"], "Cancelled")
        self.assertEqual(self.manager.get_task(second)["status"], "Cancelled")
        self.assertEqual(self.manager.get_task(other)["status"], "Queued")

    def test_bulk_events_insert_in_bounded_batches_and_clear_in_one_poll(self):
        model = self.window.task_table.model
        inserted, removed = [], []
        model.rowsInserted.connect(lambda parent, first, last: inserted.append((first, last)))
        model.rowsRemoved.connect(lambda parent, first, last: removed.append((first, last)))
        tids = self.manager.add_urls([f"https://example.com/bulk/{i}" for i in range(300)],
                                     TaskOptions(folder=str(self.folder)))
        self.window.controller.poll()
        self.assertEqual(model.rowCount(), 100)
        self.assertFalse(self.manager.events.empty())
        while not self.manager.events.empty():
            self.window.controller.poll()
        self.assertEqual(inserted, [(0, 99), (100, 199), (200, 299)])
        # Keep one active task selected while removing both surrounding queued ranges.
        survivor = tids[150]
        self.manager.update_task(survivor, status="Processing")
        self.manager.tid_to_workerid[survivor] = 1
        self.window.controller.poll()
        self.select(survivor)
        self.window.controller.clear_tasks(remove_pending=True)
        self.window.controller.poll()
        self.assertEqual(removed, [(151, 299), (0, 149)])
        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(self.window.task_table.selected_task_ids(), [survivor])
        self.assertEqual(model.index(0, 0).data(Qt.ItemDataRole.UserRole), survivor)
        self.assertEqual(self.manager.events.unfinished_tasks, 0)
        self.assertEqual(len(self.manager.list_tasks()), 1)

    def test_declining_multi_cancel_leaves_all_tasks_queued(self):
        tids = [self.add_task(), self.add_task()]
        self.select(*tids)
        self.window.cancel_action.trigger()
        self.question().done(QMessageBox.StandardButton.No)
        self.assertTrue(all(self.manager.get_task(tid)["status"] == "Queued" for tid in tids))

    def test_clear_finished_or_all_queued_requires_explicit_choice_and_keeps_files(self):
        output = self.folder / "completed.mp4"
        output.write_bytes(b"existing output")
        finished = self.add_task("Completed (MP4)", output=str(output))
        pending = self.add_task()
        self.window.clear_button.click()
        self.question().done(QMessageBox.StandardButton.Cancel)
        self.assertEqual(len(self.manager.list_tasks()), 2)
        self.window.clear_button.click()
        self.question().done(QMessageBox.StandardButton.No)
        self.assertIsNone(self.manager.get_task(finished))
        self.assertIsNotNone(self.manager.get_task(pending))
        self.window.controller.poll()
        self.window.clear_button.click()
        self.question().done(QMessageBox.StandardButton.Yes)
        self.window.controller.poll()
        self.assertEqual(self.manager.list_tasks(), [])
        self.assertEqual(self.window.task_table.model.rowCount(), 0)
        self.assertEqual(output.read_bytes(), b"existing output")

    def test_retry_scope_and_button_states_follow_sorted_selection(self):
        failed = self.add_task("Error: offline", title="Zulu")
        queued = self.add_task(title="Alpha")
        self.assertTrue(self.window.retry_action.isEnabled())
        self.assertFalse(self.window.cancel_action.isEnabled())
        self.window.task_table.view.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        self.select(queued)
        self.assertFalse(self.window.retry_action.isEnabled())
        self.assertTrue(self.window.cancel_action.isEnabled())
        self.select(failed)
        self.assertTrue(self.window.retry_action.isEnabled())
        self.assertFalse(self.window.cancel_action.isEnabled())
        with patch.object(self.manager, "retry_failed") as retry:
            self.window.retry_action.trigger()
            retry.assert_called_once_with([failed])
            retry.reset_mock()
            self.select()
            self.window.retry_action.trigger()
            retry.assert_called_once_with([])

    def test_summary_actions_and_clear_dialog_do_not_scan_all_tasks(self):
        queued = self.add_task()
        self.add_task("Error: offline")
        self.add_task()
        self.select(queued)
        with patch.object(self.manager, "list_tasks", side_effect=AssertionError("no full task scan")), \
                patch.object(self.manager, "get_task", wraps=self.manager.get_task) as get:
            self.window._refresh_actions()
            get.assert_called_once_with(queued)
            self.manager.update_task(queued, status="Downloading", progress=45)
            self.window.controller.poll()
            self.window.controller._publish_summary()
            self.window.clear_list()
            self.assertEqual(self.question().windowTitle(), "Clear task list")
            self.question().done(QMessageBox.StandardButton.Cancel)

    def test_progress_changes_do_not_refresh_action_buttons(self):
        tid = self.add_task("Downloading")
        changes = []
        self.window.controller.actions_changed.connect(lambda: changes.append(True))
        for progress in range(20):
            self.manager.update_task(tid, progress=progress, status=f"Downloading - {progress} MB")
            self.window.controller.poll()
        self.assertEqual(changes, [])
        self.manager.update_task(tid, status="Error: offline")
        self.window.controller.poll()
        self.assertEqual(changes, [True])
        self.assertTrue(self.window.retry_action.isEnabled())

    def test_failed_worker_cleanup_enables_retry_without_another_task_update(self):
        tid = self.add_task()
        failed = threading.Event()
        release = threading.Event()
        def fail_and_clean(task, worker):
            self.manager.update_task(tid, status="Error: offline")
            failed.set()
            release.wait(5)
        self.manager.engine.process_task.side_effect = fail_and_clean
        thread = threading.Thread(target=self.manager._process_task,
                                  args=((tid, self.manager.get_task(tid)["url"]), 1), daemon=True)
        thread.start()
        try:
            self.assertTrue(failed.wait(2))
            self.window.controller.poll()
            self.assertFalse(self.window.retry_action.isEnabled())
            updates, actions = [], []
            self.window.controller.task_changed.connect(lambda *args: updates.append(args))
            self.window.controller.actions_changed.connect(lambda: actions.append(True))
            release.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.window.controller.poll()
            self.assertTrue(self.window.retry_action.isEnabled())
            self.assertEqual(updates, [])
            self.assertEqual(actions, [True])
        finally:
            release.set()
            thread.join(2)

    def test_double_click_and_show_button_reveal_sorted_task_not_source_row(self):
        output = self.folder / "with spaces & symbols.mp4"
        output.touch()
        self.add_task("Error: offline", title="Zulu")
        completed = self.add_task("Completed (MP4)", title="Alpha", output=str(output))
        self.window.task_table.view.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        self.select(completed)
        self.assertTrue(self.window.show_file_action.isEnabled())
        with patch("ui.main_window.reveal_file") as reveal:
            self.window.show_file_action.trigger()
            self.window._activate_task(self.window.task_table.proxy.index(0, 1))
        self.assertEqual(reveal.call_count, 2)
        self.assertTrue(all(call.args == (str(output),) for call in reveal.call_args_list))
        output.unlink()
        with patch.object(self.window, "_show_message") as message:
            self.window.show_selected_file()
        self.assertEqual(message.call_args.args[1], "Could not show file")

    def test_failed_task_diagnostics_follow_proxy_index(self):
        self.add_task(title="Zulu")
        failed = self.add_task("Error: specific offline error", title="Alpha")
        self.window.task_table.view.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        with patch.object(self.window, "_show_message") as message:
            self.window._activate_task(self.window.task_table.proxy.index(0, 1))
        self.assertIn(failed, message.call_args.args[2])
        self.assertIn("specific offline error", message.call_args.args[3])


class RevealFileTests(unittest.TestCase):
    def test_platform_commands_preserve_paths_without_a_shell_and_handle_missing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder, "media & spaces.mp4")
            output.touch()
            for platform in ("win32", "darwin"):
                with self.subTest(platform=platform), patch.object(file_actions.sys, "platform", platform), \
                        patch.object(file_actions.subprocess, "Popen") as popen:
                    file_actions.reveal_file(str(output))
                    self.assertEqual(popen.call_args.args[0][-1], str(output.resolve()))
                    self.assertNotIn("shell", popen.call_args.kwargs)
            with patch.object(file_actions.sys, "platform", "linux"), \
                    patch.object(file_actions.QDesktopServices, "openUrl", return_value=False):
                with self.assertRaises(OSError):
                    file_actions.reveal_file(str(output))
            output.unlink()
            with patch.object(file_actions.subprocess, "Popen") as popen:
                with self.assertRaises(FileNotFoundError):
                    file_actions.reveal_file(str(output))
                popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
