"""
==============================================================
ui/tab_dev_console.py
Developer Debug Console — Interactive Python Interpreter
==============================================================

Provides a live Python console embedded in the Qt UI.
Engineers can introspect and control the running application
without restarting: change busload, inject frames, query DBC,
trigger UDS services, etc.

Architecture:
  QPlainTextEdit (output display)   ← append-only, no editing
  QLineEdit (input prompt)          → executes on Enter

  code.InteractiveInterpreter runs in MAIN THREAD
  (same thread as Qt event loop — safe to touch Qt objects)

  IMPORTANT: Do NOT move InteractiveInterpreter to a worker thread.
  Qt objects must only be touched from their owning thread.
  The console IS the main thread — that's its power.

Features:
  - Command history (↑/↓ arrows)
  - Tab completion (basic attribute completion)
  - Syntax-colored output (INFO / ERROR / WARNING)
  - Pre-injected namespace: hw, dbc, networks, trace_model, etc.
  - Multi-line input support (detected by InteractiveInterpreter)
  - Redirect stdout/stderr to Qt widget (StringIO capture)

Security note:
  This console has FULL Python access. Only enable in debug builds.
  Guard with: if os.environ.get("DEV_CONSOLE_ENABLED") == "1":
"""

from __future__ import annotations

import code
import io
import os
import sys
import traceback
from typing import List, Dict, Any, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSlot
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QPlainTextEdit, QLineEdit, QPushButton, QLabel,
    QGroupBox, QTextEdit, QFrame,
)
from PyQt6.QtGui import (
    QFont, QColor, QTextCursor, QTextCharFormat,
    QKeyEvent, QTextOption,
)

import logging
logger = logging.getLogger(__name__)


# ==============================================================
# OUTPUT CAPTURE
# ==============================================================

class QtOutputCapture:
    """
    Redirect stdout/stderr to a callback function.

    Used to capture InteractiveInterpreter output and display
    in QPlainTextEdit instead of terminal.

    Context manager usage:
        with QtOutputCapture(callback) as cap:
            exec("print('hello')")
        # callback("hello\\n") was called
    """

    def __init__(self, callback, stream_type: str = "stdout"):
        self._callback    = callback
        self._stream_type = stream_type
        self._original    = None

    def write(self, text: str) -> int:
        """Called when Python code prints output."""
        if text:
            self._callback(text, self._stream_type)
        return len(text)

    def flush(self):
        pass  # Required by io protocol

    def __enter__(self):
        if self._stream_type == "stdout":
            self._original = sys.stdout
            sys.stdout = self
        else:
            self._original = sys.stderr
            sys.stderr = self
        return self

    def __exit__(self, *args):
        if self._stream_type == "stdout":
            sys.stdout = self._original
        else:
            sys.stderr = self._original


# ==============================================================
# INTERPRETER WRAPPER
# ==============================================================

class DevInterpreter:
    """
    Wrapper around code.InteractiveInterpreter.

    Manages:
    - Pre-injected namespace (app objects)
    - Output capture → Qt callback
    - Multi-line input state (partial commands)
    - Command history
    """

    MAX_HISTORY = 200

    def __init__(self, namespace: Dict[str, Any], output_callback):
        """
        Args:
            namespace: Python objects to inject into REPL namespace
                       e.g. {"hw": hw_manager, "dbc": dbc_registry}
            output_callback: fn(text: str, stream: str) called with output
        """
        # Inject useful builtins + app objects
        full_ns = {
            "__name__": "__console__",
            "__doc__":  "CAN Diagnostic Tool — Developer Console",
            "help":     self._print_help,
            **namespace,
        }

        self._interp     = code.InteractiveInterpreter(locals=full_ns)
        self._output_cb  = output_callback
        self._buffer:    List[str] = []     # Multi-line buffer
        self._history:   List[str] = []     # Command history
        self._hist_idx:  int = -1           # History navigation index

    def push(self, line: str) -> bool:
        """
        Push one line to interpreter.

        Handles multi-line input (if/for/def/class etc.).
        Returns True if more input expected (inside block).
        Returns False if command was executed.

        Output is captured and sent to output_callback.
        """
        self._buffer.append(line)
        source = "\n".join(self._buffer)

        stdout_cap = QtOutputCapture(self._output_cb, "stdout")
        stderr_cap = QtOutputCapture(self._output_cb, "stderr")

        with stdout_cap, stderr_cap:
            try:
                # runsource returns True if more input needed,
                # False if command was complete (success or error)
                more = self._interp.runsource(source, "<console>")
            except SystemExit:
                self._output_cb("[SystemExit intercepted]\n", "warn")
                more = False
            except Exception as e:
                self._output_cb(traceback.format_exc(), "error")
                more = False

        if not more:
            # Command complete — save to history
            if self._buffer:
                cmd = "\n".join(self._buffer).strip()
                if cmd and (not self._history or self._history[-1] != cmd):
                    self._history.append(cmd)
                    if len(self._history) > self.MAX_HISTORY:
                        self._history.pop(0)
            self._buffer.clear()
            self._hist_idx = len(self._history)

        return more

    def history_prev(self) -> Optional[str]:
        """Navigate to previous command (↑ key)."""
        if not self._history:
            return None
        self._hist_idx = max(0, self._hist_idx - 1)
        return self._history[self._hist_idx]

    def history_next(self) -> Optional[str]:
        """Navigate to next command (↓ key)."""
        if not self._history:
            return None
        self._hist_idx = min(len(self._history), self._hist_idx + 1)
        if self._hist_idx >= len(self._history):
            return ""
        return self._history[self._hist_idx]

    def get_completions(self, text: str) -> List[str]:
        """Basic tab completion for namespace attributes."""
        ns = self._interp.locals
        if "." in text:
            obj_path, attr_prefix = text.rsplit(".", 1)
            try:
                obj = eval(obj_path, ns)  # noqa
                attrs = [
                    f"{obj_path}.{a}"
                    for a in dir(obj)
                    if a.startswith(attr_prefix)
                ]
                return attrs
            except Exception:
                return []
        else:
            return [k for k in ns.keys() if k.startswith(text)]

    def _print_help(self, obj=None):
        """Custom help that routes to console output."""
        if obj is None:
            msg = self._build_help_text()
        else:
            buf = io.StringIO()
            with open(os.devnull, "w") as devnull:
                real_stdout = sys.stdout
                sys.stdout = buf
                try:
                    help(obj)  # type: ignore[arg-type]
                finally:
                    sys.stdout = real_stdout
            msg = buf.getvalue()

        self._output_cb(msg, "info")

    def _build_help_text(self) -> str:
        ns = self._interp.locals
        obj_names = [k for k in ns.keys() if not k.startswith("_")]
        lines = [
            "═" * 60,
            "  CAN Diagnostic Tool — Developer Console",
            "═" * 60,
            "",
            "Available objects:",
        ]
        for name in sorted(obj_names):
            obj = ns[name]
            lines.append(f"  {name:<20} {type(obj).__name__}")
        lines += [
            "",
            "Examples:",
            "  hw.drain_rx_queue(10)         # Read 10 frames",
            "  hw.transmit(CanFrame(...))     # Send a frame",
            "  dbc.decode(1, 0x100, b'...')  # Decode frame",
            "  networks['Engine'].summary     # Network status",
            "  trace_model.clear()           # Clear trace",
            "",
            "History: ↑/↓ arrows",
            "═" * 60,
        ]
        return "\n".join(lines) + "\n"


# ==============================================================
# DEVELOPER CONSOLE TAB
# ==============================================================

class DevConsoleTab(QWidget):
    """
    Developer Console Tab.

    Layout:
    ┌─────────────────────────────────────────────────────┐
    │  ⚠️ DEVELOPER CONSOLE — DO NOT USE IN PRODUCTION    │
    ├───────────────────────────┬─────────────────────────┤
    │                           │  Quick Commands         │
    │   Output Display          │  [Read 100 Frames]      │
    │   (QPlainTextEdit)        │  [Print Networks]       │
    │                           │  [Clear Trace]          │
    │                           │  [HW Status]            │
    │                           │  [DBC Summary]          │
    ├───────────────────────────┴─────────────────────────┤
    │  >>> [input line_______________________________] [▶] │
    └─────────────────────────────────────────────────────┘
    """

    # Color scheme for output
    COLORS = {
        "stdout": QColor("#e0e0e0"),   # Light gray — normal output
        "stderr": QColor("#ff5252"),   # Red — errors
        "error":  QColor("#ff5252"),
        "warn":   QColor("#ffab40"),   # Orange — warnings
        "info":   QColor("#64b5f6"),   # Blue — info/system
        "input":  QColor("#a5d6a7"),   # Green — user input echo
    }

    def __init__(self, namespace: Dict[str, Any], parent=None):
        """
        Args:
            namespace: Dict of app objects injected into REPL:
                {
                    "hw":          hw_manager,
                    "dbc":         dbc_registry,
                    "networks":    networks_dict,
                    "trace_model": trace_model,
                    "CanFrame":    CanFrame,
                }
        """
        super().__init__(parent)
        self._namespace  = namespace
        self._interpreter: Optional[DevInterpreter] = None
        self._in_multiline = False

        self._build_ui()
        self._init_interpreter()
        self._print_banner()

    # ─── UI Construction ─────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Warning banner
        layout.addWidget(self._build_warning_banner())

        # Main area: splitter (output | quick commands)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: output + input
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self._output = self._build_output_widget()
        left_layout.addWidget(self._output, stretch=1)
        left_layout.addWidget(self._build_input_row())

        # Right: quick command buttons
        right = self._build_quick_commands()

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([700, 200])

        layout.addWidget(splitter, stretch=1)

    def _build_warning_banner(self) -> QLabel:
        lbl = QLabel(
            "⚠️  DEVELOPER CONSOLE — Full Python access — "
            "For debugging only, not for production use"
        )
        lbl.setStyleSheet(
            "background: #b71c1c; color: white; "
            "padding: 4px 8px; font-weight: bold;"
        )
        return lbl

    def _build_output_widget(self) -> QPlainTextEdit:
        out = QPlainTextEdit()
        out.setReadOnly(True)
        out.setMaximumBlockCount(10_000)  # Auto-prune old lines (memory limit)

        # Monospace font — essential for aligned output
        font = QFont("Consolas", 10)
        font.setStyleHint(QFont.StyleHint.Monospace)
        out.setFont(font)

        out.setStyleSheet(
            "QPlainTextEdit { "
            "background-color: #1e1e1e; "
            "color: #e0e0e0; "
            "border: 1px solid #333; "
            "}"
        )

        # Word wrap off — code output often has long lines
        out.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        return out

    def _build_input_row(self) -> QFrame:
        row = QFrame()
        h   = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)

        self._prompt_label = QLabel(">>>")
        self._prompt_label.setFont(QFont("Consolas", 10))
        self._prompt_label.setStyleSheet("color: #a5d6a7;")
        self._prompt_label.setFixedWidth(35)

        self._input = QLineEdit()
        self._input.setFont(QFont("Consolas", 10))
        self._input.setStyleSheet(
            "QLineEdit { background: #2d2d2d; color: #e0e0e0; "
            "border: 1px solid #555; padding: 2px 4px; }"
        )
        self._input.returnPressed.connect(self._on_execute)
        self._input.installEventFilter(self)   # For ↑↓ history navigation

        self._btn_exec = QPushButton("▶")
        self._btn_exec.setFixedWidth(30)
        self._btn_exec.clicked.connect(self._on_execute)

        h.addWidget(self._prompt_label)
        h.addWidget(self._input, stretch=1)
        h.addWidget(self._btn_exec)
        return row

    def _build_quick_commands(self) -> QGroupBox:
        """Pre-built buttons for common debug operations."""
        group = QGroupBox("Quick Commands")
        v = QVBoxLayout(group)

        commands = [
            ("📊 HW Status",       "print(hw.get_channel_count())"),
            ("🔌 Channels",        "print([hw.is_channel_active(i) for i in range(1,5)])"),
            ("📋 Networks",        "[print(n.summary) for n in dbc.get_all_networks()]"),
            ("🗑 Clear Trace",     "trace_model.clear(); print('Trace cleared')"),
            ("📦 Queue Depth",     "print(f'RX queue: {hw._rx_queue.qsize()} frames')"),
            ("📄 DBC Stats",       "print(f'{len(dbc)} networks registered')"),
            ("🔍 Rx 10 Frames",    "frames = hw.drain_rx_queue(10); [print(f'{f.can_id:03X} {f.data.hex()}') for f in frames]"),
            ("💾 Memory",         "import sys; print(f'Objects: {len(gc.get_objects())}') if __import__('gc') else None"),
        ]

        for label, cmd in commands:
            btn = QPushButton(label)
            btn.setFont(QFont("", 9))
            btn.clicked.connect(
                lambda checked, c=cmd: self._run_quick_command(c)
            )
            v.addWidget(btn)

        v.addStretch()
        return group

    # ─── Interpreter init ─────────────────────────────────────

    def _init_interpreter(self) -> None:
        """Create interpreter with app namespace."""
        self._interpreter = DevInterpreter(
            namespace=self._namespace,
            output_callback=self._append_output,
        )

    def _print_banner(self) -> None:
        """Print welcome message and available objects."""
        self._append_output(
            f"Python {sys.version}\n", "info"
        )
        self._append_output(
            "Type help() for available objects and examples.\n", "info"
        )

        if self._namespace:
            self._append_output(
                "Injected: " +
                ", ".join(self._namespace.keys()) + "\n",
                "info"
            )
        self._append_output("\n", "stdout")

    # ─── Execution ───────────────────────────────────────────

    @pyqtSlot()
    def _on_execute(self) -> None:
        """Execute command from input field."""
        if not self._interpreter:
            return

        line = self._input.text()
        self._input.clear()

        # Echo input with prompt
        prompt = "... " if self._in_multiline else ">>> "
        self._append_output(f"{prompt}{line}\n", "input")

        # Push to interpreter
        more = self._interpreter.push(line)

        # Update prompt for multi-line
        self._in_multiline = more
        self._prompt_label.setText("..." if more else ">>>")

    def _run_quick_command(self, cmd: str) -> None:
        """Execute a quick command button."""
        if not self._interpreter:
            return
        self._append_output(f">>> {cmd}\n", "input")
        self._interpreter.push(cmd)

    # ─── Output display ──────────────────────────────────────

    def _append_output(self, text: str, stream: str = "stdout") -> None:
        """
        Append colored text to output widget.

        Must be called from main thread only.
        (InteractiveInterpreter runs in main thread — always safe here)
        """
        if not text:
            return

        cursor = self._output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        fmt = QTextCharFormat()
        color = self.COLORS.get(stream, self.COLORS["stdout"])
        fmt.setForeground(color)

        cursor.setCharFormat(fmt)
        cursor.insertText(text)

        # Auto-scroll output to bottom
        self._output.setTextCursor(cursor)
        self._output.ensureCursorVisible()

    # ─── Key events for history navigation ───────────────────

    def eventFilter(self, obj, event) -> bool:
        """
        Intercept ↑/↓ keys in input field for history navigation.
        Also intercepts Tab for completion.
        """
        if obj is self._input and isinstance(event, QKeyEvent):
            if event.type() == event.Type.KeyPress:
                if event.key() == Qt.Key.Key_Up:
                    cmd = self._interpreter.history_prev() if self._interpreter else None
                    if cmd is not None:
                        self._input.setText(cmd)
                        self._input.end(False)
                    return True

                elif event.key() == Qt.Key.Key_Down:
                    cmd = self._interpreter.history_next() if self._interpreter else None
                    if cmd is not None:
                        self._input.setText(cmd)
                        self._input.end(False)
                    return True

                elif event.key() == Qt.Key.Key_Tab:
                    self._do_completion()
                    return True

        return super().eventFilter(obj, event)

    def _do_completion(self) -> None:
        """Tab completion for input field."""
        if not self._interpreter:
            return

        text = self._input.text()
        completions = self._interpreter.get_completions(text)

        if len(completions) == 1:
            # Single match — complete it
            self._input.setText(completions[0])
        elif len(completions) > 1:
            # Multiple matches — show them
            self._append_output(
                "\n".join(completions) + "\n", "info"
            )

    # ─── Public API ──────────────────────────────────────────

    def update_namespace(self, updates: Dict[str, Any]) -> None:
        """Update interpreter namespace at runtime."""
        if self._interpreter:
            self._interpreter._interp.locals.update(updates)
            self._append_output(
                f"[Namespace updated: {list(updates.keys())}]\n",
                "info"
            )

    def execute_command(self, cmd: str) -> None:
        """Execute a command programmatically."""
        self._append_output(f">>> {cmd}\n", "input")
        if self._interpreter:
            self._interpreter.push(cmd)

    def clear_output(self) -> None:
        """Clear output display."""
        self._output.clear()
        self._print_banner()