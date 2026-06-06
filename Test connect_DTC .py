import os
import sys
import time

APP_VERSION = "3.4 — Optimized Sync Iso-TP"

# ─────────────────────────────────────────────────────────────────────────────
# TERMINAL BOX HELPERS
# ─────────────────────────────────────────────────────────────────────────────

W = 66


def clear_console():
    os.system("cls" if os.name == "nt" else "clear")


def box_top():    print("┌" + "─" * W + "┐")


def box_bot():    print("└" + "─" * W + "┘")


def box_sep():    print("├" + "─" * W + "┤")


def box_blank():  box_line("")


def box_line(text: str = "", align: str = "left", pad: int = 2):
    inner = W - pad * 2
    s = text.center(inner) if align == "center" else text.ljust(inner)
    if len(s) > inner:
        s = s[: inner - 1] + "…"
    print("│" + " " * pad + s + " " * pad + "│")


def section_header(title: str):
    box_top()
    box_line(f"  {title}")
    box_sep()


def result_line(idx, label, detail=""):
    box_line(f"  [{idx:>2}]  {label}")
    if detail:
        box_line(f"         {detail}")


def warn_line(msg): box_line(f"  ⚠  {msg}")


def ok_line(msg):   box_line(f"  ✔  {msg}")


def err_line(msg):  box_line(f"  ✘  {msg}")


def info_line(msg): box_line(f"     {msg}")


def header_banner():
    box_top()
    box_blank()
    box_line("UDS Tool  —  CAN Hardware Scanner", align="center")
    box_line(f"v{APP_VERSION}    |    python-can backend", align="center")
    box_blank()
    box_bot()


# ─────────────────────────────────────────────────────────────────────────────
# HARDWARE SCANNING
# ─────────────────────────────────────────────────────────────────────────────

def check_can_lib():
    try:
        import can
        return can
    except ImportError:
        return None


def scan_vector(can) -> list:
    try:
        from can.interfaces.vector import VectorBus
        raw = VectorBus._detect_available_configs()
    except Exception as e:
        return [{"error": str(e)}]

    if not raw:
        return []

    seen, results = set(), []

    for cfg in raw:
        raw_name = cfg.get("name", "").strip()
        hw_type = cfg.get("hw_type", 0)
        hw_index = cfg.get("hw_index", 0)
        hw_channel = cfg.get("hw_channel", 0)
        ch_logic = cfg.get("channel", 0)

        sig = (hw_type, hw_index, hw_channel)
        if sig in seen:
            continue
        seen.add(sig)

        if hw_type == 1 or "virtual" in raw_name.lower():
            hw_display = "Virtual Hardware"
            is_virtual = True
        elif hw_type == 55:
            hw_display = "Vector VN1610/VN1630A"
            is_virtual = False
        elif hw_type == 59:
            hw_display = "Vector VN1640"
            is_virtual = False
        else:
            hw_display = raw_name if raw_name else f"Vector Device (Type {hw_type})"
            is_virtual = False

        results.append({
            "vendor": "vector",
            "interface": "vector",
            "hw_name": hw_display,
            "hw_type": hw_type,
            "hw_index": hw_index,
            "hw_channel": hw_channel,
            "ch_logic": ch_logic,
            "is_virtual": is_virtual,
        })

    return results


def scan_peak(can) -> list:
    try:
        from can.interfaces.pcan import PcanBus
        raw = PcanBus.detect_available_configs()
        if raw:
            return [
                {
                    "vendor": "peak",
                    "interface": "pcan",
                    "hw_name": "PEAK PCAN",
                    "channel": cfg.get("channel", ""),
                    "fd": cfg.get("fd", False),
                }
                for cfg in raw
            ]
    except Exception:
        pass
    return []


# ─────────────────────────────────────────────────────────────────────────────
# DISPLAY & INPUT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def display_scan_results(all_channels: list):
    section_header("HARDWARE AVAILABLE — Danh sách kênh phần cứng")
    for ch in all_channels:
        idx = ch["_idx"]
        name = ch["hw_name"]
        if ch["vendor"] == "vector":
            label = f"{name}  [Cổng vật lý: CH{ch['hw_channel'] + 1}]"
            detail = (
                f"interface=vector  |  "
                f"hw_type={ch['hw_type']}  hw_index={ch['hw_index']}  "
                f"hw_channel={ch['hw_channel']}"
            )
        else:
            label = f"{name}  {ch['channel']}"
            detail = f"interface=pcan"
        result_line(idx, label, detail)
    box_bot()


def prompt_channel_select(all_channels: list) -> dict | None:
    valid_ids = {ch["_idx"] for ch in all_channels}
    while True:
        print()
        box_top()
        box_line("STEP 1: CHỌN KÊNH KẾT NỐI")
        box_sep()
        info_line("• Nhập số thứ tự kênh phần cứng cần thao tác")
        info_line("• Nhập  q  để thoát ứng dụng")
        box_bot()

        raw = input(" Chọn kênh > ").strip().lower()
        if raw == "q":
            return None
        try:
            idx = int(raw)
            if idx in valid_ids:
                return next(ch for ch in all_channels if ch["_idx"] == idx)
            print("  ✘ Số thứ tự không tồn tại. Vui lòng chọn lại.")
        except ValueError:
            print("  ✘ Vui lòng chỉ nhập 1 con số.")


def prompt_bitrate() -> int:
    while True:
        print()
        box_top()
        box_line("STEP 2: CẤU HÌNH BITRATE")
        box_sep()
        info_line("  [1] 125,000 bps")
        info_line("  [2] 250,000 bps")
        info_line("  [3] 500,000 bps  ← Mặc định (nhấn ENTER)")
        info_line("  [4] 1,000,000 bps")
        box_bot()

        raw = input(" Chọn Bitrate [Enter = 500k] > ").strip()
        mapping = {"": 500_000, "1": 125_000, "2": 250_000, "3": 500_000, "4": 1_000_000}
        if raw in mapping:
            return mapping[raw]
        try:
            val = int(raw)
            if 10_000 <= val <= 1_000_000:
                return val
        except ValueError:
            pass
        print("  ✘ Giá trị không hợp lệ.")


def prompt_uds_ids() -> tuple[int, int]:
    print()
    box_top()
    box_line("STEP 3: CẤU HÌNH UDS CAN ID  (định dạng HEX)")
    box_sep()
    info_line("Ví dụ tiêu chuẩn: Request=7E0  Response=7E8")
    box_blank()
    box_bot()

    while True:
        req_raw = input(" Request ID  (Tx) [Mặc định: 7E0] > ").strip()
        resp_raw = input(" Response ID (Rx) [Mặc định: 7E8] > ").strip()
        try:
            req_id = int(req_raw, 16) if req_raw else 0x7E0
            resp_id = int(resp_raw, 16) if resp_raw else 0x7E8
            return req_id, resp_id
        except ValueError:
            print("  ✘ Giá trị hex không hợp lệ, nhập lại.")


# ─────────────────────────────────────────────────────────────────────────────
# BUS FACTORY
# ─────────────────────────────────────────────────────────────────────────────

def open_bus(can, ch: dict, bitrate: int, resp_id: int):
    is_ext = (resp_id > 0x7FF)
    filters = [{"can_id": resp_id, "can_mask": 0x1FFFFFFF if is_ext else 0x7FF, "extended": is_ext}]

    if ch["interface"] == "vector":
        return can.interface.Bus(
            interface="vector",
            channel=ch["hw_channel"],
            hw_type=ch["hw_type"],
            hw_index=ch["hw_index"],
            hw_channel=ch["hw_channel"],
            app_name=None,
            bitrate=bitrate,
            can_filters=filters
        )
    else:
        return can.interface.Bus(
            interface="pcan",
            channel=ch["channel"],
            bitrate=bitrate,
            can_filters=filters
        )


# ─────────────────────────────────────────────────────────────────────────────
# SYNC RESPONSE ASSEMBLER (Xử lý gộp gói tin UDS Đồng bộ)
# ─────────────────────────────────────────────────────────────────────────────

def receive_uds_response(can, bus, req_id: int, resp_id: int, timeout: float = 2.0) -> bool:
    """
    Hàm chặn dòng xử lý và chỉ đợi gom phản hồi UDS (Single/Multi-Frame)
    ngay sau khi gửi Tx. Kết thúc gom hoặc hết timeout sẽ nhường quyền lại terminal.
    Đồng thời in chi tiết từng Frame cấu thành trong quá trình truyền dữ liệu lớn.

    ĐÃ FIX: Hỗ trợ bắt NRC 78 (Response Pending), tự động kéo dài timeout (5.0s)
    và chờ cho tới khi ECU trả về kết quả cuối cùng.
    """
    p2_max = timeout
    p2_star_max = 5.0  # Thời gian chờ tối đa theo tiêu chuẩn khi ECU báo bận (P2* server max)
    current_timeout = p2_max

    start_time = time.time()
    is_assembling = False
    expected_length = 0
    buffer = bytearray()
    next_sn = 1

    # Chốt chặn an toàn tránh vòng lặp vô hạn nếu ECU bị treo cứng gửi 78 liên tục
    nrc78_counter = 0
    max_nrc78_allowed = 20

    while (time.time() - start_time) < current_timeout:
        try:
            msg = bus.recv(timeout=0.05)
            if not msg or msg.arbitration_id != resp_id:
                continue

            data = msg.data
            if not data or len(data) < 1:
                continue

            pci = data[0]
            frame_type = (pci & 0xF0) >> 4
            frame_hex = " ".join(f"{b:02X}" for b in data)

            # ---- CASE 1: SINGLE FRAME (0x0X) ----
            if frame_type == 0:
                length = pci & 0x0F
                if length > 7 or length == 0:
                    continue
                payload = data[1: 1 + length]

                # ─── KIỂM TRA PHẢN HỒI TIÊU CỰC (NEGATIVE RESPONSE - 0x7F) ───
                if len(payload) >= 3 and payload[0] == 0x7F:
                    nrc = payload[2]

                    if nrc == 0x78:  # Gặp NRC 78: RequestCorrectlyReceived-ResponsePending
                        nrc78_counter += 1
                        if nrc78_counter > max_nrc78_allowed:
                            print(
                                f"  ✘ [Rx Error] ECU gửi NRC 78 liên tục vượt quá {max_nrc78_allowed} lần. Ngắt kết nối!")
                            return False

                        print(f"  📥 [Rx] NRC 78 (Response Pending) -> [{frame_hex}]")
                        print(f"     └─> ECU cần thêm thời gian xử lý. Tự động gia hạn P2* Timeout ({p2_star_max}s)...")

                        # Cập nhật thời gian chờ mới và reset mốc tính thời gian
                        current_timeout = p2_star_max
                        start_time = time.time()
                        continue  # Bỏ qua logic bên dưới, quay lại đầu vòng lặp tiếp tục đợi

                    else:
                        # Các mã NRC lỗi thực tế khác (Ví dụ: 22 - ConditionsNotCorrect, 31 - OutOfRange...)
                        print(f"  📥 [Rx] Negative Response (NRC 0x{nrc:02X}) -> [{frame_hex}]")
                        return False

                # ─── PHẢN HỒI SINGLE FRAME THÀNH CÔNG (POSITIVE RESPONSE) ───
                raw_hex = " ".join(f"{b:02X}" for b in payload)
                print(f"  📥 [Rx] Single-Frame Data: [{raw_hex}]")
                return True

            # ---- CASE 2: FIRST FRAME (0x1X) ----
            elif frame_type == 1:
                if len(data) < 2:
                    continue
                expected_length = ((pci & 0x0F) << 8) | data[1]
                buffer = bytearray(data[2:])
                is_assembling = True
                next_sn = 1

                print(f"  📥 [Rx] [FF] First Frame -> [{frame_hex}] (Độ dài tổng: {expected_length} bytes)")

                # Phản hồi Flow Control (FC) nhanh lập tức để ECU đẩy tiếp dữ liệu
                fc_data = bytes([0x30, 0x00, 0x00, 0xAA, 0xAA, 0xAA, 0xAA, 0xAA])
                fc_msg = can.Message(
                    arbitration_id=req_id,
                    data=fc_data,
                    is_extended_id=(req_id > 0x7FF)
                )
                bus.send(fc_msg)

                print(f"  📤 [Tx] [FC] Flow Control Sent -> [30 00 00 AA AA AA AA AA]")

                # Khi đã bắt đầu nhận chuỗi Multi-frame, trả timeout về mức chuẩn ban đầu (p2_max)
                current_timeout = p2_max
                start_time = time.time()

            # ---- CASE 3: CONSECUTIVE FRAME (0x2X) ----
            elif frame_type == 2:
                if not is_assembling:
                    continue

                sn = pci & 0x0F
                print(f"  📥 [Rx] [CF] Consecutive Frame (SN: {sn}) -> [{frame_hex}]")

                if sn != next_sn:
                    print(f"  ✘ [Rx Error] Lệch Sequence Number! Nhận: {sn}, Đợi: {next_sn}")
                    return False

                rem_bytes = expected_length - len(buffer)
                take_bytes = min(7, rem_bytes)
                buffer.extend(data[1: 1 + take_bytes])
                next_sn = (next_sn + 1) & 0x0F

                current_timeout = p2_max
                start_time = time.time()

                if len(buffer) >= expected_length:
                    print(f"  ------------------------------------------------------------------")
                    final_payload = buffer[:expected_length]
                    raw_hex = " ".join(f"{b:02X}" for b in final_payload)
                    print(f"  📥 [Rx] Multi-Frame Complete — Total Bytes: {expected_length}")
                    print(f"  [RAW] {raw_hex}")
                    return True

        except Exception as e:
            print(f"  ✘ Lỗi trong quá trình nhận dữ liệu: {e}")
            return False

    print("  ⚠ Timeout: Không nhận được phản hồi UDS từ ECU.")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# UDS TERMINAL
# ─────────────────────────────────────────────────────────────────────────────

def run_uds_terminal(can, bus, ch: dict, req_id: int, resp_id: int):
    clear_console()
    header_banner()

    print()
    box_top()
    box_line("UDS CONSOLE TERMINAL  —  ACTIVE", align="center")
    box_sep()
    if ch["vendor"] == "vector":
        info_line(
            f" Kênh : {ch['hw_name']}  (Cổng vật lý: CH{ch['hw_channel'] + 1})"
            f"  hw_type={ch['hw_type']}  hw_index={ch['hw_index']}"
        )
    else:
        info_line(f" Kênh : {ch['hw_name']}  {ch['channel']}")
    info_line(f" CAN  : Request 0x{req_id:03X}  →  Response 0x{resp_id:03X}")
    box_sep()
    info_line(" Nhập SID + Sub-function liền mạch hoặc cách khoảng trắng.")
    info_line("   Ví dụ:  1902FF   hoặc   19 02 FF   →  tự đóng gói Single Frame")
    info_line(" Gõ  exit  hoặc Ctrl+C để thoát phiên.")
    box_bot()
    print()

    try:
        while True:
            raw_in = input(" uds_terminal > ").strip().lower()
            if not raw_in:
                continue
            if raw_in == "exit":
                break

            cleaned = raw_in.replace(" ", "")
            try:
                payload = bytes.fromhex(cleaned)
            except ValueError:
                print("  ✘ Hex không hợp lệ! Ví dụ: 1902FF hoặc 10 01")
                continue

            if len(payload) > 7:
                print("  ✘ Hiện tại Tool chỉ hỗ trợ GỬI lệnh dạng Single Frame (<= 7 Bytes).")
                continue

            # Đóng gói ISO-TP Single Frame chuẩn
            frame_data = bytes([len(payload)]) + payload
            frame_data = frame_data.ljust(8, b"\xAA")

            # Xả sạch/Lọc bỏ các gói tin rác tồn đọng trong bộ đệm Rx trước khi bắn lệnh Tx mới đi
            while bus.recv(timeout=0.0) is not None:
                pass

            tx_msg = can.Message(
                arbitration_id=req_id,
                data=frame_data,
                is_extended_id=(req_id > 0x7FF),
            )
            bus.send(tx_msg)
            tx_hex = " ".join(f"{b:02X}" for b in frame_data)
            print(f"  📤 [Tx] ID: 0x{req_id:03X} | Data: [{tx_hex}]")

            # Ngay lập tức nhảy vào hàm chờ phản hồi đồng bộ, nhận đủ tin mới nhả prompt nhập tiếp theo
            receive_uds_response(can, bus, req_id, resp_id, timeout=1.5)

    except KeyboardInterrupt:
        pass
    finally:
        bus.shutdown()
        print("\n  ✔ Đã đóng phiên làm việc an toàn.")
        time.sleep(0.8)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if sys.platform == "win32":
        os.system("")

    can = check_can_lib()
    if not can:
        box_top()
        err_line("python-can chưa được cài đặt!")
        info_line("Chạy:  pip install python-can")
        box_bot()
        sys.exit(1)

    while True:
        clear_console()
        header_banner()

        print("\n  [i] Đang quét phần cứng CAN...")
        vec_raw = scan_vector(can)
        peak_raw = scan_peak(can)

        all_channels: list[dict] = []
        idx = 1
        for ch in vec_raw:
            if "error" in ch:
                continue
            ch["_idx"] = idx
            all_channels.append(ch)
            idx += 1
        for ch in peak_raw:
            ch["_idx"] = idx
            all_channels.append(ch)
            idx += 1

        if not all_channels:
            print()
            box_top()
            warn_line("Không tìm thấy thiết bị CAN nào được kết nối.")
            box_bot()
            input("\n Nhấn Enter để quét lại hoặc  q + Enter  để thoát... ")
            continue

        display_scan_results(all_channels)

        ch = prompt_channel_select(all_channels)
        if ch is None:
            break

        bitrate = prompt_bitrate()
        req_id, resp_id = prompt_uds_ids()

        name_display = (
            f"{ch['hw_name']} CH{ch['hw_channel'] + 1}"
            if ch["vendor"] == "vector"
            else f"{ch['hw_name']} {ch['channel']}"
        )
        print(f"\n  Đang mở kênh  {name_display}  @ {bitrate:,} bps ...")

        try:
            bus = open_bus(can, ch, bitrate, resp_id)
            print(f"  ✔ Kết nối thành công!\n")
            run_uds_terminal(can, bus, ch, req_id, resp_id)
        except Exception as e:
            print(f"\n  ✘ Lỗi kết nối: {e}")
            input("\n  Bấm Enter để quay lại...")

        print()
        box_top()
        info_line(" [Enter] Phiên mới   |   [Q] Thoát hẳn")
        box_bot()
        if input(" Lựa chọn > ").strip().lower() == "q":
            break

    print("\n  Goodbye.\n")


if __name__ == "__main__":
    main()