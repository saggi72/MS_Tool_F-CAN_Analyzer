from core.program_store import ProgramStore
from ui.generated.DiagnosticSettingWidget import Ui_DiagnosticsSettingWidget
from ui.widgets.ascii_rule_table import AsciiRuleTable
from windows.base_window import BaseWindow


class DiagnosticSettingWindow(BaseWindow):
    """Cấu hình theo Program. Không phụ thuộc Start/Stop nên không override on_active_changed."""

    def __init__(self, store: ProgramStore, parent=None):
        super().__init__(parent)
        self.ui = Ui_DiagnosticsSettingWidget()
        self.ui.setupUi(self)
        self.store = store
        self.ascii_rule = AsciiRuleTable(self.ui.tableAsciiRule)

        self.ui.comboBoxECUList.currentTextChanged.connect(self.ascii_rule.set_ecu)
        self.ui.btnSave.clicked.connect(self.on_save)
        self.ui.comboBoxProgram.currentTextChanged.connect(self.on_program_changed)
        self.reload_programs()

    def reload_programs(self):
        cb = self.ui.comboBoxProgram
        cur = cb.currentText()
        cb.blockSignals(True)
        cb.clear()
        cb.addItems(self.store.list_programs())
        cb.setCurrentText(cur)
        cb.blockSignals(False)
        self.on_program_changed(cb.currentText())

    def on_program_changed(self, program: str):
        if not program:
            return
        try:
            self.ascii_rule.load_json(self.store.ascii_path(program))
        except FileNotFoundError:
            pass

    def on_save(self):
        p = self.ui.comboBoxProgram.currentText()
        if p:
            self.ascii_rule.save_json(self.store.ascii_path(p))