"""
==============================================================
ui/tab_file_config.py
FILE Tab — Configuration Management
==============================================================

Manages:
  - NEW    : Tạo config rỗng mới
  - OPEN   : Load file .dbcconfig → emit config_loaded → AddDbcTab.load_config()
  - SAVE   : Lưu networks hiện tại (name + channel + dbc_path) vào file cũ
  - SAVE AS: Lưu với tên mới
  - EXIT   : Thoát ứng dụng

Compatible với:
  - ConfigManager  : get_config_dir(), save_config() raises, load_config() raises
  - CanNetwork     : .name, .channel_index, .dbc_file
  - DbcConfigHelper: create_config_from_networks(), verify_dbc_files()

Layout:
  ┌─────────────┬──────────────────────────────────────┐
  │  FILE MENU  │  Config Information (text)           │
  │             ├──────────────────────────────────────┤
  │  ➕ NEW     │  Config JSON (text)                  │
  │  📂 OPEN    │                                      │
  │  💾 SAVE    │                                      │
  │  💾 SAVE AS │                                      │
  │             │                                      │
  │  🚪 EXIT    │                                      │
  └─────────────┴──────────────────────────────────────┘
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional, TYPE_CHECKING

# ─── Qt5 / Qt6 compat ────────────────────────────────────────
try:
    from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
    from PyQt6.QtWidgets import (
        QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
        QLabel, QPushButton, QFileDialog, QMessageBox,
        QTextEdit, QInputDialog,
    )
    from PyQt6.QtGui import QFont
    _Qt_Horizontal  = Qt.Orientation.Horizontal
    _QMsgBox_Yes    = QMessageBox.StandardButton.Yes
    _QMsgBox_Cancel = QMessageBox.StandardButton.Cancel
except ImportError:
    from PyQt5.QtCore import Qt, pyqtSignal, pyqtSlot          # type: ignore
    from PyQt5.QtWidgets import (                               # type: ignore
        QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
        QLabel, QPushButton, QFileDialog, QMessageBox,
        QTextEdit, QInputDialog,
    )
    from PyQt5.QtGui import QFont                               # type: ignore
    _Qt_Horizontal  = Qt.Horizontal
    _QMsgBox_Yes    = QMessageBox.Yes
    _QMsgBox_Cancel = QMessageBox.Cancel

from models.config_manager import ConfigManager, DbcConfigHelper, ConfigError

if TYPE_CHECKING:
    from ui.main_window import MainWindow

logger = logging.getLogger(__name__)


class FileTab(QWidget):
    """
    FILE Tab — Configuration Manager.

    Save/Load workflow:
      1. User tạo networks + load DBC ở AddDbcTab
      2. SAVE  → collect từ main.add_dbc_tab._networks → ghi .dbcconfig (JSON)
      3. OPEN  → đọc .dbcconfig → emit config_loaded(dict)
      4. MainWindow._on_config_loaded → AddDbcTab.load_config() restore state
    """

    # Signal phát khi config được load — consumed by MainWindow
    config_loaded = pyqtSignal(dict)

    def __init__(self, main: "MainWindow", parent=None):
        super().__init__(parent)

        # Giữ tham chiếu tới MainWindow để truy cập networks khi SAVE
        self.main = main

        # Trạng thái config hiện tại
        self.current_config: Optional[dict]  = None
        self.current_filepath: Optional[str] = None

        self._build_ui()

    # ==================================================
    # UI BUILD
    # ==================================================

    def _build_ui(self) -> None:
        """Splitter layout: menu trái + display phải."""
        root_layout = QHBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(_Qt_Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setSizes([220, 800])

        root_layout.addWidget(splitter)

    # ── Left panel: menu buttons ──────────────────────────────

    def _build_left_panel(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # Title
        title = QLabel("FILE MENU")
        font = QFont()
        font.setBold(True)
        font.setPointSize(11)
        title.setFont(font)
        layout.addWidget(title)
        layout.addSpacing(10)

        # Action buttons
        actions = [
            ("➕  NEW",      self._new_config),
            ("📂  OPEN",     self._open_config),
            ("💾  SAVE",     self._save_config),
            ("💾  SAVE AS",  self._save_as_config),
        ]
        for text, handler in actions:
            btn = QPushButton(text)
            btn.setMinimumHeight(35)
            btn.clicked.connect(handler)
            layout.addWidget(btn)

        layout.addSpacing(10)

        # Exit — tách biệt bằng khoảng cách
        btn_exit = QPushButton("🚪  EXIT")
        btn_exit.setMinimumHeight(35)
        btn_exit.setStyleSheet(
            "QPushButton { color: #c62828; font-weight: bold; }"
            "QPushButton:hover { background: #ffebee; }"
        )
        btn_exit.clicked.connect(self._exit_tool)
        layout.addWidget(btn_exit)

        layout.addStretch()
        return widget

    # ── Right panel: info + JSON display ─────────────────────

    def _build_right_panel(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Config Information
        lbl_info = QLabel("Config Information")
        lbl_info.setFont(self._bold_font())
        layout.addWidget(lbl_info)

        self.info_display = QTextEdit()
        self.info_display.setReadOnly(True)
        self.info_display.setMaximumHeight(180)
        self.info_display.setFont(QFont("Consolas", 9))
        layout.addWidget(self.info_display)

        # Config JSON
        lbl_json = QLabel("Config JSON")
        lbl_json.setFont(self._bold_font())
        layout.addWidget(lbl_json)

        self.json_display = QTextEdit()
        self.json_display.setReadOnly(True)
        self.json_display.setFont(QFont("Consolas", 9))
        layout.addWidget(self.json_display, stretch=1)

        return widget

    @staticmethod
    def _bold_font() -> QFont:
        f = QFont()
        f.setBold(True)
        return f

    # ==================================================
    # FILE OPERATIONS
    # ==================================================

    def _new_config(self) -> None:
        """Tạo config rỗng mới với tên do user nhập."""
        try:
            name, ok = QInputDialog.getText(
                self, "New Configuration",
                "Enter config name:",
                text="new_config",
            )
            if not ok or not name.strip():
                return

            name = name.strip()
            self.current_config   = ConfigManager.create_empty_config(name)
            self.current_filepath = None
            self._update_displays()

            QMessageBox.information(
                self, "Config Created",
                f"New configuration '{name}' created.\n\n"
                "Now load DBCs in 'Add DBC' tab and use SAVE to write to disk.",
            )
            logger.info(f"New config created: {name}")

        except Exception as e:
            logger.error(f"Error creating config: {e}")
            QMessageBox.critical(self, "Error", f"Failed to create config:\n{e}")

    def _open_config(self) -> None:
        """Mở file .dbcconfig, emit config_loaded để MainWindow xử lý."""
        try:
            config_dir = str(ConfigManager.get_config_dir())
            filepath, _ = QFileDialog.getOpenFileName(
                self, "Open Configuration File",
                config_dir,
                "Config Files (*.dbcconfig);;All Files (*)",
            )
            if not filepath:
                return

            # load_config() raise ConfigError nếu lỗi
            try:
                config = ConfigManager.load_config(filepath)
            except ConfigError as e:
                QMessageBox.critical(self, "Error", f"Failed to load config:\n{e}")
                return

            self.current_config   = config
            self.current_filepath = filepath
            self._update_displays()

            # Kiểm tra DBC files còn tồn tại không
            file_status   = DbcConfigHelper.verify_dbc_files(config)
            missing_files = [name for name, exists in file_status.items() if not exists]

            if missing_files:
                msg = (
                    "Config loaded, but some DBC files are missing:\n\n"
                    + "\n".join(f"  - {n}" for n in missing_files)
                    + "\n\nGo to 'Add DBC' tab to reload them."
                )
                QMessageBox.warning(self, "Missing DBC Files", msg)
            else:
                net_count = len(config.get("networks", []))
                QMessageBox.information(
                    self, "Config Loaded",
                    f"Configuration loaded successfully!\n\n"
                    f"File: {Path(filepath).name}\n"
                    f"Networks: {net_count}",
                )

            # Phát tín hiệu → MainWindow._on_config_loaded → AddDbcTab.load_config()
            self.config_loaded.emit(config)
            logger.info(f"Config opened: {filepath}")

        except Exception as e:
            logger.error(f"Error opening config: {e}", exc_info=True)
            QMessageBox.critical(self, "Error", f"Failed to open config:\n{e}")

    def _save_config(self) -> None:
        """Lưu config vào file hiện tại (nếu chưa có filepath thì gọi SAVE AS)."""
        try:
            if not self.current_config:
                QMessageBox.warning(
                    self, "No Config",
                    "Please create or open a config first.",
                )
                return

            config = self._collect_current_state()

            if self.current_filepath:
                filename   = Path(self.current_filepath).stem
                saved_path = ConfigManager.save_config(config, filename)

                self.current_config   = config
                self.current_filepath = str(saved_path)
                self._update_displays()

                net_count = len(config.get("networks", []))
                QMessageBox.information(
                    self, "Saved",
                    f"Config saved successfully!\n\n{net_count} networks saved.",
                )
                logger.info(f"Config saved: {saved_path}")
            else:
                # Chưa có đường dẫn → chuyển sang Save As
                self._save_as_config()

        except ConfigError as e:
            logger.error(f"Error saving config: {e}")
            QMessageBox.critical(self, "Save Error", f"Failed to save:\n{e}")
        except Exception as e:
            logger.error(f"Error saving config: {e}", exc_info=True)
            QMessageBox.critical(self, "Error", f"Failed to save config:\n{e}")

    def _save_as_config(self) -> None:
        """Lưu config với tên mới do user nhập."""
        try:
            default_name = "config"
            if self.current_config:
                default_name = self.current_config.get("config_name", "config")

            name, ok = QInputDialog.getText(
                self, "Save Configuration As",
                "Enter filename:",
                text=default_name,
            )
            if not ok or not name.strip():
                return

            name   = name.strip()
            config = self._collect_current_state()
            config["config_name"] = name

            # save_config() raise ConfigError nếu lỗi, trả về Path nếu OK
            saved_path = ConfigManager.save_config(config, name)

            self.current_config   = config
            self.current_filepath = str(saved_path)
            self._update_displays()

            networks_list = "\n".join(
                f"  • {n.get('name')} (CH{n.get('channel', '?')}): "
                f"{n.get('dbc_path') or '(no DBC)'}"
                for n in config.get("networks", [])
            )
            QMessageBox.information(
                self, "Saved",
                f"Config saved as '{name}{ConfigManager.CONFIG_EXTENSION}'\n\n"
                f"Location: {saved_path.parent}\n\n"
                f"Networks:\n{networks_list}",
            )
            logger.info(f"Config saved as: {saved_path}")

        except ConfigError as e:
            logger.error(f"Error in save as: {e}")
            QMessageBox.critical(self, "Save Error", f"Failed to save:\n{e}")
        except Exception as e:
            logger.error(f"Error in save as: {e}", exc_info=True)
            QMessageBox.critical(self, "Error", f"Failed to save config:\n{e}")

    def _exit_tool(self) -> None:
        """Thoát ứng dụng sau khi xác nhận."""
        reply = QMessageBox.question(
            self, "Exit",
            "Exit MS Tool?\n\nMake sure to save your config first.",
            _QMsgBox_Yes | _QMsgBox_Cancel,
            _QMsgBox_Cancel,
        )
        if reply == _QMsgBox_Yes:
            logger.info("Application exiting via FILE tab EXIT button.")
            sys.exit(0)

    # ==================================================
    # COLLECT STATE FROM MAIN
    # ==================================================

    def _collect_current_state(self) -> dict:
        """
        Thu thập networks hiện tại từ AddDbcTab vào config dict.

        Ưu tiên lấy từ self.main.add_dbc_tab._networks (AddDbcTab).
        Fallback về self.main.networks nếu MainWindow expose riêng.

        Dùng DbcConfigHelper.create_config_from_networks() — gọi
        CanNetwork.to_dict() nội bộ để capture đúng name/channel/dbc_path.
        """
        config_name = "config"
        if self.current_config:
            config_name = self.current_config.get("config_name", "config")

        # Ưu tiên lấy từ AddDbcTab._networks
        networks = {}
        if hasattr(self.main, "add_dbc_tab") and \
                hasattr(self.main.add_dbc_tab, "_networks"):
            networks = self.main.add_dbc_tab._networks

        # Fallback: main.networks (nếu MainWindow expose trực tiếp)
        elif hasattr(self.main, "networks") and self.main.networks:
            networks = self.main.networks

        if not networks:
            config = ConfigManager.create_empty_config(config_name)
            logger.info("No networks to save — empty config created")
            return config

        config = DbcConfigHelper.create_config_from_networks(
            config_name, networks
        )
        logger.info(
            f"Collected {len(config['networks'])} networks: "
            + ", ".join(
                f"{n['name']}(CH{n.get('channel', '?')})"
                for n in config["networks"]
            )
        )
        return config

    # ==================================================
    # DISPLAY UPDATE
    # ==================================================

    def _update_displays(self) -> None:
        """Cập nhật info panel và JSON panel sau mỗi thao tác file."""
        if not self.current_config:
            self.info_display.clear()
            self.json_display.clear()
            return

        networks  = self.current_config.get("networks", [])
        created   = self.current_config.get("created", "")
        filename  = (
            Path(self.current_filepath).name
            if self.current_filepath
            else "(Not saved yet)"
        )

        # ── Info section ──────────────────────────────────────
        lines = [
            "Config Information",
            "",
            f"File:     {filename}",
            f"Networks: {len(networks)}",
            f"Created:  {created[:19] if created else 'N/A'}",
            "",
            "Network List:",
        ]
        for net in networks:
            name    = net.get("name", "")
            channel = net.get("channel")
            path    = net.get("dbc_path", "")
            ch_str  = f"CH{channel}" if channel else "No CH"

            if path and os.path.exists(path):
                status = "✓"
            elif path:
                status = "✗ (missing)"
            else:
                status = "- (no DBC)"

            lines.append(f"  {status}  {name} ({ch_str})")
            if path:
                lines.append(f"       Path: {path}")

        self.info_display.setText("\n".join(lines))

        # ── JSON section ──────────────────────────────────────
        self.json_display.setText(
            json.dumps(self.current_config, indent=2, ensure_ascii=False)
        )