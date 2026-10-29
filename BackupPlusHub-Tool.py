#!/usr/bin/env python3

"""
Seagate Backup Plus Hub —— 灯光开关 / 休眠时间 查询与设置工具

这个工具能做什么:
1. 自动寻找希捷 Backup Plus Hub (读硬盘自报的厂商/型号)
2. 查询与设置 灯光开关 / 休眠时间

本脚本需要 Windows 管理员权限：
因为工具要绕过文件系统，直接向硬盘发底层 SCSI 命令。
脚本会自动弹 UAC 提权，它只对选中的那块硬盘发命令，不会读写你的文件。


机制（从 Seagate Dashboard 4.9.1 逆向得到）
------------------------------------------------------------
工具直接问硬盘本身，而不是通过 Windows 文件系统：

1. 灯光
   查询：命令码 FA, 读回 4 字节
   设置：命令码 F9, 发送 4 字节

   第 1 字节 = 第一路灯光状态: 0x02 = 亮, 0x01 = 灭
   第 2 字节 = 第二路灯光状态: 本设备没有第二个灯，始终是 0xFF
   读回来的第 3/4 字节固定是 0x55 0xAA (固件标记)

2. 休眠时间
   读写硬盘内部的一张 "电源管理设置表" (SCSI 标准页号 0x1A)
   这张表里有两项：
     - 休眠开关: 1 = 开启休眠, 0 = 永不休眠
     - 休眠时间：硬盘空闲多久后进入待机，单位是 100 毫秒
                 例如 30 分钟 > 1800 秒 > 存储数值为 18000

   读取用 MODE SENSE(6)，写入用 MODE SELECT(6) 并带 "保存到设备" 标志
   设置会写进硬盘自身，拔插、重启后依然有效。

   可选档位 (Seagate Dashboard):
     永不休眠
     3、4、5、10、15、30 分钟
     1、2、3、4、5 小时
     自定义秒数


技术细节
--------------------
LED 查询 CDB: FA 00 00 00 00 00 04 00 00 00    data-in  4 字节
LED 设置 CDB: F9 00 00 00 00 00 04 00 00 00    data-out 4 字节
              发送的数据: 02/01, FF, 00, 00

休眠读取 CDB: 1A 08 1A 00 FF 00      MODE SENSE(6)
休眠写入 CDB: 15 11 00 00 <len> 00   MODE SELECT(6), PF=1、SP=1 (保存_

设置页 0x1A 的布局 (偏移从这张表的第 1 个字节算起):
  字节 0     = 页号
               只有低 6 位是真正的页号 0x1A
               最高位是 "可保存(PS)" 标志
               所以真机返回 0x9A ( = 0x1A | 0x80)
               判断页号要用 字节 & 0x3F

  字节 3     = 休眠开关
               bit0: 1 = 开启休眠, 0 = 永不休眠

  字节 8..11 = 休眠倒计时数值
               4 个字节 big endian, 单位 100ms
               例如 30 分钟 -> 18000 -> 00 00 46 50
"""

import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3

IOCTL_SCSI_PASS_THROUGH_DIRECT = 0x0004D014

SENSE_OFF = 0x40  # sense 缓冲区在 SPT 缓冲内的偏移
BUF_SIZE = SENSE_OFF + 32  # x86/x64 结构体 + 32 字节 sense
MAX_DISK_SCAN = 32  # 扫描 PhysicalDrive0..31

CDB_LED_GET = bytes([0xFA, 0x00, 0x00, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00])
CDB_LED_SET = bytes([0xF9, 0x00, 0x00, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00])
CDB_MODE_SENSE_1A = bytes([0x1A, 0x08, 0x1A, 0x00, 0xFF, 0x00])
CDB_WAKE = bytes([0x1B, 0x00, 0x00, 0x00, 0x71, 0x00])
CDB_INQUIRY = bytes([0x12, 0x00, 0x00, 0x00, 0x60, 0x00])

# Dashboard 的休眠档位（秒），None 表示永不休眠
SLEEP_OPTIONS = (
    [(None, "永不休眠")]
    + [(m * 60, f"{m} 分钟") for m in (3, 4, 5, 10, 15, 30)]
    + [(h * 3600, f"{h} 小时") for h in range(1, 6)]
)

# ----------------------------------------------------------------------
# Windows API / SCSI 结构体
# ----------------------------------------------------------------------
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class SPT_DIRECT(ctypes.Structure):
    """对应 Windows 的 SCSI_PASS_THROUGH_DIRECT（ctypes 默认对齐与编译器一致）。"""

    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("ScsiStatus", ctypes.c_ubyte),
        ("PathId", ctypes.c_ubyte),
        ("TargetId", ctypes.c_ubyte),
        ("Lun", ctypes.c_ubyte),
        ("CdbLength", ctypes.c_ubyte),
        ("SenseInfoLength", ctypes.c_ubyte),
        ("DataIn", ctypes.c_ubyte),
        ("DataTransferLength", ctypes.c_uint32),
        ("TimeOutValue", ctypes.c_uint32),
        ("DataBuffer", ctypes.c_void_p),
        ("SenseInfoOffset", ctypes.c_uint32),
        ("Cdb", ctypes.c_ubyte * 16),
    ]


kernel32.CreateFileW.argtypes = [
    ctypes.c_wchar_p,
    wt.DWORD,
    wt.DWORD,
    ctypes.c_void_p,
    wt.DWORD,
    wt.DWORD,
    ctypes.c_void_p,
]
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.DeviceIoControl.argtypes = [
    ctypes.c_void_p,
    wt.DWORD,
    ctypes.c_void_p,
    wt.DWORD,
    ctypes.c_void_p,
    wt.DWORD,
    ctypes.POINTER(wt.DWORD),
    ctypes.c_void_p,
]
kernel32.DeviceIoControl.restype = ctypes.c_int
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.CloseHandle.restype = ctypes.c_int
kernel32.SetConsoleTitleW.argtypes = [ctypes.c_wchar_p]
kernel32.SetConsoleTitleW.restype = ctypes.c_int

shell32.IsUserAnAdmin.argtypes = []
shell32.IsUserAnAdmin.restype = ctypes.c_int
shell32.ShellExecuteW.argtypes = [
    ctypes.c_void_p,
    ctypes.c_wchar_p,
    ctypes.c_wchar_p,
    ctypes.c_wchar_p,
    ctypes.c_wchar_p,
    ctypes.c_int,
]
shell32.ShellExecuteW.restype = ctypes.c_void_p


# ----------------------------------------------------------------------
# 基础：权限 / 提权 / 控制台
# ----------------------------------------------------------------------
def is_admin():
    try:
        return bool(shell32.IsUserAnAdmin())
    except Exception:
        return False


def set_console_title():
    try:
        kernel32.SetConsoleTitleW("Seagate Backup Plus Hub - LED / Sleep Tool")
    except Exception:
        pass


def elevate():
    """以管理员身份重新启动本脚本；返回 True 表示已成功拉起。"""
    script = os.path.abspath(sys.argv[0])
    try:
        rc = shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{script}"', os.getcwd(), 1
        )
    except Exception as exc:
        print(f"提权调用失败: {exc}")
        return False
    if not rc or int(rc) <= 32:
        print(f"提权失败或被取消（ShellExecuteW 返回 {rc}）。")
        return False
    return True


def pause():
    try:
        input("\n按回车退出...")
    except (EOFError, KeyboardInterrupt):
        pass


# ----------------------------------------------------------------------
# SCSI 透传
# ----------------------------------------------------------------------
class ScsiError(Exception):
    pass


def open_drive(disk_no):
    """打开 \\\\.\\PhysicalDriveN；成功返回 (handle, 0)，失败返回 (None, win32_error)。"""
    path = f"\\\\.\\PhysicalDrive{disk_no}"
    handle = kernel32.CreateFileW(
        path,
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    err = ctypes.get_last_error()
    if not handle or handle == INVALID_HANDLE_VALUE:
        return None, err
    return handle, 0


def close_handle(handle):
    if handle:
        try:
            kernel32.CloseHandle(handle)
        except Exception:
            pass


def scsi_send(handle, cdb, data_len=0, data_in=True, timeout=10, out_data=None):
    """
    发一条 SCSI 命令。
    返回 (ok, scsi_status, sense_bytes, in_data, win32_error)
    """
    spt_buf = ctypes.create_string_buffer(BUF_SIZE)
    spt = ctypes.cast(spt_buf, ctypes.POINTER(SPT_DIRECT)).contents

    data_buf = None
    if data_len > 0:
        data_buf = ctypes.create_string_buffer(data_len)
        if not data_in:
            if out_data is None or len(out_data) != data_len:
                raise ValueError("data-out 长度不匹配")
            ctypes.memmove(data_buf, bytes(out_data), data_len)

    spt.Length = ctypes.sizeof(SPT_DIRECT)
    spt.ScsiStatus = 0
    spt.PathId = 0
    spt.TargetId = 1  # 与 Dashboard 的写法一致
    spt.Lun = 0
    spt.CdbLength = len(cdb)
    spt.SenseInfoLength = 32
    spt.DataIn = 1 if data_in else 0
    spt.DataTransferLength = data_len
    spt.TimeOutValue = timeout
    spt.DataBuffer = ctypes.addressof(data_buf) if data_buf is not None else 0
    spt.SenseInfoOffset = SENSE_OFF
    for i, b in enumerate(cdb):
        spt.Cdb[i] = b

    returned = wt.DWORD(0)
    ok = kernel32.DeviceIoControl(
        handle,
        IOCTL_SCSI_PASS_THROUGH_DIRECT,
        ctypes.cast(spt_buf, ctypes.c_void_p),
        BUF_SIZE,
        ctypes.cast(spt_buf, ctypes.c_void_p),
        BUF_SIZE,
        ctypes.byref(returned),
        None,
    )
    winerr = ctypes.get_last_error()
    sense = spt_buf.raw[SENSE_OFF : SENSE_OFF + 32]
    in_data = None
    if data_buf is not None and data_in:
        in_data = data_buf.raw[:data_len]

    return bool(ok), int(spt.ScsiStatus), sense, in_data, winerr


def scsi_or_raise(handle, cdb, data_len=0, data_in=True, timeout=10, out_data=None):
    ok, status, sense, in_data, winerr = scsi_send(
        handle, cdb, data_len, data_in, timeout, out_data
    )
    if not ok or status != 0:
        sense_key = sense[2] & 0x0F if len(sense) > 2 else 0
        asc = sense[12] if len(sense) > 13 else 0
        ascq = sense[13] if len(sense) > 13 else 0
        sense_hex = " ".join(f"{b:02X}" for b in sense[:18])
        raise ScsiError(
            f"SCSI 命令失败: SCSI状态=0x{status:02X}, Win32={winerr}, "
            f"SenseKey=0x{sense_key:X}, ASC/ASCQ=0x{asc:02X}/0x{ascq:02X}, "
            f"Sense={sense_hex}"
        )
    return in_data


# ----------------------------------------------------------------------
# 设备识别
# ----------------------------------------------------------------------
def inquiry(handle):
    """返回 (vendor, product, revision)。"""
    data = scsi_or_raise(handle, CDB_INQUIRY, 96, True, 10)
    vendor = data[8:16].decode("latin-1", "replace").strip()
    product = data[16:32].decode("latin-1", "replace").strip()
    revision = data[32:36].decode("latin-1", "replace").strip()
    return vendor, product, revision


def is_hub(vendor, product):
    """Backup Plus Hub 的 INQUIRY 一般形如: Seagate / Backup+ Hub BK。"""
    text = (f"{vendor} {product}").lower()
    return ("seagate" in text) and ("hub" in text)


def scan_disks(verbose=True):
    """
    扫描 PhysicalDrive0..N，返回 (entries, hub)。
    entries: [{index, vendor, product, revision}]
    hub:     第一个匹配 Backup Plus Hub 的 entry（找不到为 None）
    """
    entries = []
    hub = None
    for n in range(MAX_DISK_SCAN):
        handle, _err = open_drive(n)
        if not handle:
            continue
        try:
            try:
                vendor, product, revision = inquiry(handle)
            except ScsiError:
                vendor, product, revision = "(INQUIRY失败)", "", ""
        finally:
            close_handle(handle)
        entry = {"index": n, "vendor": vendor, "product": product, "revision": revision}
        entries.append(entry)
        if verbose:
            print(f"    PhysicalDrive{n:<2d} : {vendor:<10} {product:<26} {revision}")
        if hub is None and is_hub(vendor, product):
            hub = entry
    return entries, hub


def find_hub_interactive():
    """检测 Hub；未插入时让用户选择重试或退出。返回 hub entry 或 None。"""
    while True:
        print("\n[1/3] 正在检测是否插入希捷 Backup Plus Hub ...")
        entries, hub = scan_disks(verbose=True)
        if not entries:
            print(
                "    [!] 没有枚举到任何物理磁盘：可能没有管理员权限，或被系统策略限制。"
            )
        if hub is not None:
            print(
                f"    [OK] 检测到 Backup Plus Hub: PhysicalDrive{hub['index']}  "
                f"({hub['vendor']} {hub['product']}, 固件 {hub['revision']})"
            )
            return hub
        print("    [x] 未检测到希捷 Backup Plus Hub。")
        try:
            ans = input("        检查 USB 连接/供电后重试： [r] 重新检测   [q] 退出 : ")
        except (EOFError, KeyboardInterrupt):
            return None
        if ans.strip().lower() == "q":
            return None


# ----------------------------------------------------------------------
# LED
# ----------------------------------------------------------------------
def led_code_text(code):
    if code == 0x01:
        return "关"
    if code == 0x02:
        return "开"
    if code == 0xFF:
        return "不支持/保持不变"
    return f"未知(0x{code:02X})"


def decode_led(data):
    """Backup Plus Hub 只有一路 LED，只取第 1 字节。"""
    return led_code_text(data[0])


def get_led(handle):
    data = scsi_or_raise(handle, CDB_LED_GET, 4, True, 10)
    if data is None or len(data) < 4:
        raise ScsiError(f"GET LED 返回数据长度异常: {data!r}")
    return data


def set_led(handle, on):
    """设置 LED（只处理第 1 路；第 2 字节固定 0xFF=不处理）。"""
    code = 2 if on else 1
    payload = bytes([code, 0xFF, 0x00, 0x00])
    scsi_or_raise(handle, CDB_LED_SET, 4, False, 10, payload)
    time.sleep(0.25)
    return get_led(handle)


def wake_disk(handle):
    """Dashboard 在读取电源设置前会先发 START STOP UNIT 唤醒磁盘。"""
    scsi_or_raise(handle, CDB_WAKE, 0, False, 20)


def get_led_with_wake(handle):
    try:
        return get_led(handle)
    except ScsiError:
        wake_disk(handle)
        time.sleep(0.5)
        return get_led(handle)


# ----------------------------------------------------------------------
# 休眠时间（MODE SENSE/SELECT page 0x1A）
# ----------------------------------------------------------------------
def decode_power(resp):
    """解析 MODE SENSE 返回，返回 dict。"""
    if resp is None or len(resp) < 5:
        raise ScsiError("MODE SENSE 返回数据过短")
    page_offset = 4 + 8 * resp[3]
    if page_offset < 4 or page_offset + 12 > len(resp):
        raise ScsiError("MODE SENSE 返回中找不到 page 0x1A 的位置")
    page_code_raw = resp[page_offset]
    # 页号在低 6 位：bit7=PS(可保存)，bit6=SPF(子页格式)。
    # 真机(Backup+ Hub BK)返回的是 0x9A = 0x1A | PS，所以不能直接和 0x1A 比较。
    page_code = page_code_raw & 0x3F
    if page_code != 0x1A:
        raise ScsiError(
            "MODE SENSE 返回的不是 page 0x1A（页字节=0x{:02X}，页号=0x{:02X}，前 24 字节={}）".format(
                page_code_raw,
                page_code,
                " ".join(f"{b:02X}" for b in resp[:24]),
            )
        )
    enabled = bool(resp[page_offset + 3] & 0x01)
    raw = (
        (resp[page_offset + 8] << 24)
        | (resp[page_offset + 9] << 16)
        | (resp[page_offset + 10] << 8)
        | resp[page_offset + 11]
    )
    return {
        "enabled": enabled,
        "raw": raw,
        "seconds": (raw // 10) if enabled else None,
        "page_offset": page_offset,
        "response": bytes(resp),
    }


def fmt_power(info):
    if info is None:
        return "读取失败"
    if not info["enabled"]:
        return "永不休眠 (Never)"
    seconds = info["seconds"]
    if seconds % 3600 == 0:
        return f"{seconds // 3600} 小时 ({seconds} 秒)"
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟 ({seconds} 秒)"
    return f"{seconds} 秒"


def read_power_raw(handle):
    resp = scsi_or_raise(handle, CDB_MODE_SENSE_1A, 255, True, 10)
    return decode_power(resp)


def get_power_with_wake(handle):
    try:
        return read_power_raw(handle)
    except ScsiError:
        wake_disk(handle)
        time.sleep(0.5)
        return read_power_raw(handle)


def set_power(handle, seconds):
    """设置休眠时间。seconds 为 None 表示永不休眠；返回设置后读回的 dict。"""
    current = read_power_raw(handle)
    resp = current["response"]
    total = resp[0] + 1
    if total < 16 or total > 255:
        raise ScsiError(f"MODE SENSE 返回的参数表长度异常: {total}")

    page_offset = current["page_offset"]
    param_list = bytearray(resp[:total])
    param_list[0] = 0x00  # MODE SELECT 时 mode data length 字节必须为 0

    if seconds is None:
        param_list[page_offset + 3] = 0x00  # 休眠开关 = 0：永不休眠
    else:
        param_list[page_offset + 3] = (
            0x01  # 休眠开关 = 1：开启自动休眠（与 Dashboard 写法一致）
        )
        raw = (seconds * 10) & 0xFFFFFFFF
        param_list[page_offset + 8] = (raw >> 24) & 0xFF
        param_list[page_offset + 9] = (raw >> 16) & 0xFF
        param_list[page_offset + 10] = (raw >> 8) & 0xFF
        param_list[page_offset + 11] = raw & 0xFF

    # CDB: PF=1, SP=1 -> 0x11（保存到设备 NVRAM）
    cdb = bytes([0x15, 0x11, 0x00, 0x00, total, 0x00])
    try:
        scsi_or_raise(handle, cdb, total, False, 6, bytes(param_list))
    except ScsiError:
        # 有些固件不支持保存页，退化为只改当前值（SP=0）
        cdb_no_save = bytes([0x15, 0x10, 0x00, 0x00, total, 0x00])
        scsi_or_raise(handle, cdb_no_save, total, False, 6, bytes(param_list))
        print("    [!] 设备不支持保存设置(SP=1)，本次仅临时生效(SP=0)。")

    time.sleep(0.2)
    return read_power_raw(handle)


# ----------------------------------------------------------------------
# 状态显示 / 菜单
# ----------------------------------------------------------------------
def print_device_status(hub, led_data, led_error, power_info, power_error):
    print("\n================ 当前状态 ================")
    print(
        f"设备 : PhysicalDrive{hub['index']}   "
        f"{hub['vendor']} {hub['product']}   (固件 {hub['revision']})"
    )
    if led_error:
        print(f"LED  : 读取失败 —— {led_error}")
    else:
        print(f"LED  : {decode_led(led_data)}")
    if power_error:
        print(f"休眠 : 读取失败 —— {power_error}")
    else:
        print(f"休眠 : {fmt_power(power_info)}")
    print("==========================================")


def print_main_menu():
    print("\n请选择操作：")
    print("  1) 开灯")
    print("  2) 关灯")
    print("  3) 修改休眠时间")
    print("  0) 退出")


def do_led(handle, on):
    label = "开灯" if on else "关灯"
    print(f"\n正在{label} ...")
    try:
        before = get_led(handle)
        print(f"  操作前: {decode_led(before)}")
        after = set_led(handle, on)
        print(f"  操作后: {decode_led(after)}")
        if after[0] == (2 if on else 1):
            print(f"  [OK] LED 已{label}")
        else:
            print("  [!] 命令已发送，但读回状态不符合预期。")
    except ScsiError as exc:
        print(f"  [x] 操作失败: {exc}")
        print(
            "  提示: 确认设备是 Backup Plus Hub 且固件支持该 LED 命令；设备忙时稍后重试。"
        )


def do_sleep_menu(handle):
    while True:
        print("\n---- 修改休眠时间 ----")
        for i, (_, label) in enumerate(SLEEP_OPTIONS, 1):
            print(f"  {i:2d}) {label}")
        print("  99) 自定义秒数")
        print("   0) 返回")
        try:
            choice = input("请输入序号: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if choice == "0":
            return

        seconds = None
        label = None
        if choice == "99":
            raw = input("请输入休眠秒数（例如 1800 = 30 分钟，1-86400）: ").strip()
            if not raw.isdigit():
                print("无效输入。")
                continue
            seconds = int(raw)
            if seconds < 1 or seconds > 86400:
                print("请输入 1 到 86400 之间的整数。")
                continue
            label = f"{seconds} 秒（自定义）"
        elif choice.isdigit() and 1 <= int(choice) <= len(SLEEP_OPTIONS):
            seconds, label = SLEEP_OPTIONS[int(choice) - 1]
        else:
            print("无效输入，请重新选择。")
            continue

        print(f"\n正在设置休眠时间: {label} ...")
        try:
            info = set_power(handle, seconds)
            print("  [OK] 设置完成")
            if seconds is not None and info["enabled"] and info["seconds"] != seconds:
                print(
                    f"  [!] 读回值与请求值不一致（请求 {seconds} 秒，"
                    f"读回 {info['seconds']}）。"
                )
        except ScsiError as exc:
            print(f"  [x] 设置失败: {exc}")


def device_status(handle):
    """读取当前 LED 与休眠设置，返回 (led_data, led_error, power_info, power_error)。"""
    led_data = led_error = None
    power_info = power_error = None
    try:
        led_data = get_led_with_wake(handle)
    except ScsiError as exc:
        led_error = str(exc)
    try:
        power_info = get_power_with_wake(handle)
    except ScsiError as exc:
        power_error = str(exc)
    return led_data, led_error, power_info, power_error


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def main():
    set_console_title()
    
    if not is_admin():
        print("该工具需要管理员权限")
        print("正在请求 UAC 提权，请在弹窗中点“是” ...")
        if not elevate():
            pause()
        return

    hub = find_hub_interactive()
    if hub is None:
        print("\n已退出。")
        return

    handle, err = open_drive(hub["index"])
    if not handle:
        print(f"\n[x] 打开 PhysicalDrive{hub['index']} 失败, Win32={err}")
        pause()
        return

    try:
        print("\n[2/3] 正在读取当前灯光状态与休眠时间 ...")
        led_data, led_error, power_info, power_error = device_status(handle)
        print_device_status(hub, led_data, led_error, power_info, power_error)

        print("\n[3/3] 可以开始操作了。")
        while True:
            print_main_menu()
            try:
                choice = input("请输入 1/2/3/0: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n已退出。")
                return

            if choice == "0":
                print("已退出。")
                return
            if choice == "1":
                do_led(handle, True)
            elif choice == "2":
                do_led(handle, False)
            elif choice == "3":
                do_sleep_menu(handle)
            else:
                print("无效输入，请重新选择。")
                continue

            led_data, led_error, power_info, power_error = device_status(handle)
            print_device_status(hub, led_data, led_error, power_info, power_error)
    finally:
        close_handle(handle)


if __name__ == "__main__":
    main()
