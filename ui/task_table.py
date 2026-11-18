"""Sortable task table with stable task IDs and painted progress bars."""
from PySide6.QtCore import QSortFilterProxyModel, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHeaderView, QLabel, QMenu,
    QStyledItemDelegate, QStyle, QStyleOptionProgressBar, QStyleOptionViewItem,
    QTableView, QVBoxLayout, QWidget,
)

from ui.task_model import PROGRESS_ROLE, SORT_ROLE, TaskTableModel
from ui.i18n import register


class ProgressDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        style = option.widget.style() if option.widget else QApplication.style()
        background = QStyleOptionViewItem(option)
        self.initStyleOption(background, index)
        background.text = ""
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, background, painter, option.widget)
        bar = QStyleOptionProgressBar()
        bar.rect = option.rect.adjusted(5, 6, -5, -6)
        bar.state = option.state | QStyle.StateFlag.State_Horizontal
        bar.palette = option.palette
        bar.direction = option.direction
        bar.minimum = 0
        bar.maximum = 100
        bar.progress = round(index.data(PROGRESS_ROLE) or 0)
        bar.text = index.data(Qt.ItemDataRole.DisplayRole)
        bar.textVisible = True
        bar.textAlignment = Qt.AlignmentFlag.AlignCenter
        style.drawControl(QStyle.ControlElement.CE_ProgressBar, bar, painter, option.widget)


class TaskTable(QWidget):
    selection_changed = Signal()
    show_file_requested = Signal(str)
    diagnostics_requested = Signal(str)
    cancel_requested = Signal(list)
    retry_requested = Signal(list)
    HEADERS = TaskTableModel.HEADERS

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.view = QTableView(self)
        self.view.setAccessibleName("Download tasks")
        self.model = TaskTableModel(self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(SORT_ROLE)
        self.proxy.setDynamicSortFilter(True)
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setAlternatingRowColors(True)
        self.view.setWordWrap(False)
        self.view.setItemDelegateForColumn(5, ProgressDelegate(self.view))
        self.view.verticalHeader().hide()
        self.view.verticalHeader().setDefaultSectionSize(34)
        header = self.view.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(65)
        self.view.setColumnWidth(0, 70)
        self.view.setColumnWidth(4, 170)
        self.view.setColumnWidth(5, 120)
        self.view.selectionModel().selectionChanged.connect(lambda *_: self.selection_changed.emit())
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._open_context_menu)
        self._context_menu = QMenu(self)
        layout.addWidget(self.view)
        self.empty_label = QLabel("Your queue is empty. Paste a video URL above to get started.", self)
        self.empty_label.setObjectName("mutedLabel")
        layout.addWidget(self.empty_label)
        register(self)

    def update_task(self, tid, task):
        self.update_tasks([(tid, task)])

    def update_tasks(self, tasks):
        self.model.update_tasks(tasks)
        self.empty_label.setVisible(self.model.rowCount() == 0)

    def remove_task(self, tid):
        self.remove_tasks([tid])

    def remove_tasks(self, tids):
        self.model.remove_tasks(tids)
        self.empty_label.setVisible(self.model.rowCount() == 0)

    def selected_task_ids(self):
        return [index.data(Qt.ItemDataRole.UserRole)
                for index in self.view.selectionModel().selectedRows(0)]

    def _open_context_menu(self, position):
        index = self.view.indexAt(position)
        if not index.isValid():
            return
        if not self.view.selectionModel().isRowSelected(index.row()):
            self.view.selectRow(index.row())
        tid = index.data(Qt.ItemDataRole.UserRole)
        task = self.model.task_for_id(tid)
        tids = self.selected_task_ids()
        tasks = [self.model.task_for_id(selected) for selected in tids]
        self._context_menu.clear()
        show = self._context_menu.addAction("Show in folder")
        show.setEnabled(bool(task and task.get("output_path") and task["status"].startswith("Completed")))
        show.triggered.connect(lambda: self.show_file_requested.emit(tid))
        details = self._context_menu.addAction("View diagnostics")
        details.setEnabled(bool(task and (task["status"].startswith("Error") or task.get("warnings"))))
        details.triggered.connect(lambda: self.diagnostics_requested.emit(tid))
        self._context_menu.addSeparator()
        cancel = self._context_menu.addAction("Cancel selected")
        cancel.setEnabled(any(t and not t["status"].startswith(("Completed", "Error", "Cancel")) for t in tasks))
        cancel.triggered.connect(lambda: self.cancel_requested.emit(tids))
        retry = self._context_menu.addAction("Retry selected")
        retry.setEnabled(any(t and t["status"].startswith("Error") for t in tasks))
        retry.triggered.connect(lambda: self.retry_requested.emit(tids))
        register(self._context_menu)
        self._context_menu.popup(self.view.viewport().mapToGlobal(position))
