"""ProgramStore: nguồn dữ liệu duy nhất cho Program (Console, DID, DTC cùng dùng).

<root>/<Program>/
    <Program>_DID_Config.json
    <Program>_ASCIIRule.json
    <Program>_DTC/<ECU>_DTC.json
Không có thông tin network: bus/loại CAN do user chọn ở nơi khác.
"""
import json
import os
import re
import shutil
from PyQt6.QtCore import QObject, pyqtSignal

RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10))}
NAME_RE = re.compile(r"^[A-Za-z0-9_\- ]{1,64}$")

EMPTY_DID_CONFIG = {"schema_version": 1, "functional_id": "0x7DF", "ecus": []}
# ecus: [{"name","request_id","response_id","id_format"(auto/11-bit/29-bit),
#         "security_dll"(tương đối nếu nằm trong thư mục Program),
#         "odx_source", "services":[{"name","request","revision_request","enabled"}]}]


def validate_name(name: str) -> str:
    """Trả về chuỗi lỗi, rỗng nếu hợp lệ."""
    if not NAME_RE.match(name):
        return "Chỉ dùng chữ, số, _, -, dấu cách; tối đa 64 ký tự."
    if name.endswith((" ", ".")):
        return "Tên không được kết thúc bằng dấu cách hoặc dấu chấm."
    if name.upper() in RESERVED:
        return "Tên dành riêng của Windows."
    return ""


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    if os.path.exists(path):
        os.replace(path, path + ".bak")
    os.replace(tmp, path)


class ProgramStore(QObject):
    programs_changed = pyqtSignal()
    program_saved = pyqtSignal(str)

    def __init__(self, root: str):
        super().__init__()
        self.root = root
        os.makedirs(root, exist_ok=True)

    # --- đường dẫn ---
    def folder(self, p):       return os.path.join(self.root, p)
    def did_path(self, p):     return os.path.join(self.folder(p), f"{p}_DID_Config.json")
    def ascii_path(self, p):   return os.path.join(self.folder(p), f"{p}_ASCIIRule.json")
    def dtc_dir(self, p):      return os.path.join(self.folder(p), f"{p}_DTC")

    # --- danh sách / tạo / xóa / đổi tên ---
    def list_programs(self):
        return sorted(d for d in os.listdir(self.root)
                      if os.path.isdir(self.folder(d)))

    def create(self, name: str):
        err = validate_name(name)
        if err:
            raise ValueError(err)
        if os.path.exists(self.folder(name)):
            raise FileExistsError(f"Program '{name}' đã tồn tại.")
        os.makedirs(self.dtc_dir(name))
        _write_json(self.did_path(name), EMPTY_DID_CONFIG)
        _write_json(self.ascii_path(name), {"All": []})
        self.programs_changed.emit()

    def delete(self, name: str):
        shutil.rmtree(self.folder(name))
        self.programs_changed.emit()

    def rename(self, old: str, new: str, copy: bool = False):
        """Đổi tên (hoặc copy) cả thư mục lẫn các file có tiền tố Program."""
        err = validate_name(new)
        if err:
            raise ValueError(err)
        if os.path.exists(self.folder(new)):
            raise FileExistsError(f"Program '{new}' đã tồn tại.")
        (shutil.copytree if copy else shutil.move)(self.folder(old), self.folder(new))
        for dirpath, dirs, files in os.walk(self.folder(new), topdown=False):
            for n in files + dirs:
                if n.startswith(old + "_"):
                    os.replace(os.path.join(dirpath, n),
                               os.path.join(dirpath, new + n[len(old):]))
        self.programs_changed.emit()

    # --- nạp / lưu ---
    def load_did(self, p):
        """Tìm theo hậu tố *_DID_Config.json, tránh lệch tên khi thư mục bị copy tay."""
        path = self.did_path(p)
        if not os.path.exists(path):
            cand = [f for f in os.listdir(self.folder(p)) if f.endswith("_DID_Config.json")]
            if not cand:
                return dict(EMPTY_DID_CONFIG)
            path = os.path.join(self.folder(p), cand[0])
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def save_did(self, p, cfg: dict):
        _write_json(self.did_path(p), cfg)
        self.program_saved.emit(p)

    def save_dtc(self, p, ecu: str, obj):
        os.makedirs(self.dtc_dir(p), exist_ok=True)
        _write_json(os.path.join(self.dtc_dir(p), f"{ecu}_DTC.json"), obj)

    def load_dtc(self, p, ecu: str):
        """Nhận cả dạng danh sách trần lẫn object {"source","created","items"}."""
        path = os.path.join(self.dtc_dir(p), f"{ecu}_DTC.json")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data["items"] if isinstance(data, dict) else data