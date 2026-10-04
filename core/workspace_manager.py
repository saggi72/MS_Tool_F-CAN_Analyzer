from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QInputDialog, QMdiArea, QMessageBox, QTabWidget,
    QToolButton, QVBoxLayout, QWidget,
)

MDI_BACKGROUND = QColor(238, 246, 255)   # cùng màu đã đặt trong Designer


class WorkspaceManager(QObject):
    """Quản lý các Desktop (mỗi tab = một QMdiArea độc lập) của workspaceTabs."""

    workspace_changed = pyqtSignal(object)   # phát QMdiArea đang được chọn

    def __init__(self, tabs: QTabWidget, parent=None):
        super().__init__(parent)
        self._tabs = tabs

        self._install_add_button()
        tabs.tabCloseRequested.connect(self.close_workspace)
        tabs.tabBar().tabBarDoubleClicked.connect(self.rename_workspace)
        tabs.currentChanged.connect(
            lambda _index: self.workspace_changed.emit(self.current_mdi()))

    # ---------- truy vấn ----------
    def mdi_at(self, index: int) -> QMdiArea | None:
        page = self._tabs.widget(index)
        return page.findChild(QMdiArea) if page else None

    def current_mdi(self) -> QMdiArea | None:
        return self.mdi_at(self._tabs.currentIndex())

    def all_mdis(self) -> list[QMdiArea]:
        return [m for i in range(self._tabs.count())
                if (m := self.mdi_at(i)) is not None]

    # ---------- thêm / đóng / đổi tên ----------
    def add_workspace(self, name: str | None = None) -> QMdiArea:
        name = name or self._next_name()
        key = name.replace(" ", "")

        page = QWidget()
        page.setObjectName(f"page{key}")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        area = QMdiArea()
        area.setObjectName(f"mdiArea{key}")
        area.setBackground(QBrush(MDI_BACKGROUND))
        layout.addWidget(area)

        index = self._tabs.addTab(page, name)
        self._tabs.setCurrentIndex(index)
        return area

    def close_workspace(self, index: int):
        if self._tabs.count() <= 1:               # luôn giữ ít nhất 1 Desktop
            return
        area = self.mdi_at(index)
        if area is not None and area.subWindowList():
            answer = QMessageBox.question(
                self._tabs, "Close Desktop",
                f"'{self._tabs.tabText(index)}' still has open windows.\n"
                "Close this desktop anyway?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        page = self._tabs.widget(index)
        self._tabs.removeTab(index)
        page.deleteLater()

    def rename_workspace(self, index: int):
        if index < 0:                              # double-click vào vùng trống
            return
        name, ok = QInputDialog.getText(
            self._tabs, "Rename Desktop", "Name:",
            text=self._tabs.tabText(index))
        if ok and name.strip():
            self._tabs.setTabText(index, name.strip())

    # ---------- lệnh Layout (chỉ tác động Desktop hiện tại) ----------
    def cascade(self):
        if (area := self.current_mdi()) is not None:
            area.cascadeSubWindows()

    def tile(self):
        if (area := self.current_mdi()) is not None:
            area.tileSubWindows()

    def minimize_all(self):
        if (area := self.current_mdi()) is not None:
            for sub in area.subWindowList():
                sub.showMinimized()

    # ---------- nội bộ ----------
    def _install_add_button(self):
        btn = QToolButton()
        btn.setObjectName("addDesktopButton")
        btn.setText("+")
        btn.setAutoRaise(True)
        btn.setToolTip("Add desktop")
        btn.clicked.connect(lambda: self.add_workspace())
        self._tabs.setCornerWidget(btn, Qt.Corner.BottomRightCorner)

    def _next_name(self) -> str:
        used = {self._tabs.tabText(i) for i in range(self._tabs.count())}
        n = 1
        while f"Desktop {n}" in used:
            n += 1
        return f"Desktop {n}"