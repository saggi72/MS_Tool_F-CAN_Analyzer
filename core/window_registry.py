from dataclasses import dataclass
from typing import Callable

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QMdiArea, QMdiSubWindow, QWidget

from core.workspace_manager import WorkspaceManager


@dataclass(frozen=True)
class WindowSpec:
    key: str                              # định danh duy nhất của loại cửa sổ
    title: str
    factory: Callable[[], QWidget]
    size: tuple[int, int] = (800, 500)
    single_instance: bool = True          # mỗi Desktop chỉ 1 cửa sổ loại này


class WindowRegistry(QObject):
    """Ánh xạ objectName của QAction -> cửa sổ, mở vào MDI của Desktop hiện tại."""

    window_created = pyqtSignal(object)   # widget vừa được tạo và hiển thị (không phát khi chỉ kích hoạt lại)

    def __init__(self, workspaces: WorkspaceManager, parent=None):
        super().__init__(parent)
        self._workspaces = workspaces
        self._specs: dict[str, WindowSpec] = {}

    # ---------- đăng ký ----------
    def register(self, action_name: str, spec: WindowSpec):
        self._specs[action_name] = spec

    def registered_actions(self) -> set[str]:
        return set(self._specs)

    def bind(self, ui):
        """Nối các action đã đăng ký với ui (đối tượng Ui_MainWindow)."""
        for action_name in self._specs:
            action = getattr(ui, action_name)
            action.triggered.connect(
                lambda _checked=False, n=action_name: self.open(n))

    # ---------- mở cửa sổ ----------
    def open(self, action_name: str) -> QMdiSubWindow | None:
        spec = self._specs.get(action_name)
        area = self._workspaces.current_mdi()
        if spec is None or area is None:
            return None

        if spec.single_instance:
            existing = self._find(area, spec.key)
            if existing is not None:
                self._activate(area, existing)
                return existing

        widget = spec.factory()
        sub = area.addSubWindow(widget)
        sub.setWindowTitle(spec.title)
        sub.setProperty("window_key", spec.key)
        sub.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        sub.resize(*spec.size)
        sub.show()
        self.window_created.emit(widget)
        return sub

    # ---------- nội bộ ----------
    @staticmethod
    def _find(area: QMdiArea, key: str) -> QMdiSubWindow | None:
        for sub in area.subWindowList():
            if sub.property("window_key") == key:
                return sub
        return None

    @staticmethod
    def _activate(area: QMdiArea, sub: QMdiSubWindow):
        if sub.isMinimized():
            sub.showNormal()
        area.setActiveSubWindow(sub)