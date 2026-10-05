from core.program_store import ProgramStore
from ui.generated.UDSConsoleWidget import Ui_ConsoleTab
from windows.base_window import BaseWindow


class UdsConsoleWindow(BaseWindow):
    def __init__(self, store: ProgramStore, diag_hw_manager, parent=None):
        super().__init__(parent)
        self.ui = Ui_ConsoleTab()
        self.ui.setupUi(self)
        self.store = store
        self.diag_hw = diag_hw_manager
        self.ui.btnSend.setEnabled(False)       # chỉ gửi được khi đang chạy
        self.reload_programs()

    def reload_programs(self):
        cb = self.ui.comboBoxProgram
        cb.clear()
        cb.addItems(self.store.list_programs())

    def on_active_changed(self, active: bool):
        self.ui.btnSend.setEnabled(active)
        # TODO: tạo / dừng UdsEngine tại đây