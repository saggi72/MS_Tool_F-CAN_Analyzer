from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QStyle

ICON_DIR = Path(__file__).resolve().parent.parent / "resources" / "icons"


def load_icon(name: str, style: QStyle, fallback: QStyle.StandardPixmap) -> QIcon:
    """SVG trong resources/icons nếu có, không thì icon chuẩn của Qt."""
    path = ICON_DIR / f"{name}.svg"
    if path.exists():
        icon = QIcon(str(path))
        if not icon.isNull():
            return icon
    return style.standardIcon(fallback)