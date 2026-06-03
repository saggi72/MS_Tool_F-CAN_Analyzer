"""
ui/tab_help.py — Help Tab
"""
from __future__ import annotations
import logging
from typing import TYPE_CHECKING

try:
    from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QTextBrowser
    from PyQt6.QtGui import QFont
except ImportError:
    from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel, QTextBrowser  # type: ignore
    from PyQt5.QtGui import QFont                                            # type: ignore

if TYPE_CHECKING:
    from ui.main_window import MainWindow

logger = logging.getLogger(__name__)

HELP_HTML = """
<h2>MS Tool — CAN Configuration & Diagnostic Manager</h2>
<h3>Luồng sử dụng cơ bản:</h3>
<ol>
  <li><b>File</b> — Load hoặc lưu file cấu hình <code>.dbcconfig</code></li>
  <li><b>Add DBC</b> — Thêm network, assign channel, load file <code>.dbc</code></li>
  <li><b>HW connect</b> — Kết nối Vector XL / PEAK PCAN / Virtual channel</li>
  <li><b>Trace</b> — Xem CAN frame realtime với decode DBC</li>
  <li><b>SendCAN</b> — Gửi CAN frame thủ công hoặc định kỳ</li>
  <li><b>UDS</b> — Thực hiện các dịch vụ UDS ISO 14229 (Session, Security, ReadDID, DTC...)</li>
</ol>
<h3>Phím tắt:</h3>
<ul>
  <li>Tất cả tab điều hướng qua menu bar phía trên</li>
  <li>Dev Console: đặt <code>DEV_CONSOLE_ENABLED=1</code> trước khi chạy</li>
</ul>
<h3>Yêu cầu phần cứng:</h3>
<ul>
  <li>Vector: cài Vector XL Driver Library + <code>vxlapi64.dll</code></li>
  <li>PEAK: cài PCAN-Basic Driver + <code>PCANBasic.dll</code></li>
  <li>Virtual: không cần phần cứng — dùng để phát triển UI</li>
</ul>
"""


class HelpTab(QWidget):
    def __init__(self, main_window: "MainWindow", parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)

        browser = QTextBrowser()
        browser.setHtml(HELP_HTML)
        browser.setOpenExternalLinks(True)
        layout.addWidget(browser)