"""原生 FFT 实时频谱 — PyQt6 平滑折线图。

matplotlib 只能 ~30 FPS，换 PyQt6 自绘可到 180 FPS 平滑；黑底、极简文字、渐变填充的
原生 FFT 折线。无边框窗口 + 主题皮肤 + dB 定义域滚动 + 自动隐藏工具条 + max-hold
包络录制（多段异色对比，用于验证 EQ 预设是否生效）。

用法:
    python spectrum.py                 # 自动用「立体声混音」采集系统播放的声音
    python spectrum.py --list          # 列出所有输入设备
    python spectrum.py --device 9      # 指定输入设备索引
    python spectrum.py --config x      # CAVA 风格配置（含 [theme] / [color]）

交互:
    滚轮              = 垂直滚动 dB 定义域（-140..0 dBFS）
    Ctrl+滚轮          = y 轴缩放（10dB 单元格高度；默认=最大单元格，滚轮下缩小至全 0..140，滚轮上放大）
    左键拖动          = 移动窗口（无边框；贴任务栏模式下靠近底部会被吸附回任务栏）
    鼠标悬停顶部      = 弹出工具条
    R                 = 录制 / 停止 max-hold 包络
    C                 = 清空所有包络（同时停止正在进行的录制）
    L                 = 对数 / 线性频率轴切换
    N                 = 音高（乐理）标注开关
    O                 = 循环窗口透明度（100/70/50/25%，默认 25%）
    Esc               = 退出

工具条（dock，悬停底部弹出）:
    Trans 关 = 实底模式，内容透明度自动变 85%（开时恢复原值，默认 25%；dock/折线恒不透明）
    Hide    = 隐藏 dock 10 秒（期间悬停底部也不弹出）
    slider knob 颜色跟随主题（line_top）

(Trans, Top) 模式联动（2026-08-20）:
    top 开 = 置顶：trans 开 → 盖任务栏（底边贴全屏底，浮在任务栏/开始面板之上）
                  trans 关 → 贴任务栏（窗口底边跟随任务栏上沿，弹出上移、收起下移，紧贴）
    top 关 = 强制预览：高度300 / tint80（与 trans 状态无关）；再开 top 恢复默认（100/8）
    top 切换（无论开/关）→ trans 自动关闭（2026-08-20 优化）
"""
import argparse
import configparser
import ctypes
from ctypes import wintypes
import math
import os
import sys
import time

# Win32：64 位下 HWND_TOPMOST=(HWND)-1 是 64 位全 1；不声明 argtypes 会被 ctypes 按 32 位 int
# 传递（截断成 0xFFFFFFFF），Windows 不识别 → SetWindowPos 返回 0 失败 → 置顶不生效。
# 声明 argtypes 后特殊 HWND 值按 64 位指针宽度传递。
user32 = ctypes.windll.user32
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
# 点击穿透：GWL_EXSTYLE=-20，WS_EX_TRANSPARENT=0x20 让鼠标事件穿透到下层窗口
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x20
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_longlong
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
user32.SetWindowLongPtrW.restype = ctypes.c_longlong
# 贴任务栏跟随：查询 Shell_TrayWnd 当前可见区域（自动隐藏弹出/收起）
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL

import numpy as np
import sounddevice as sd
from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import (QBrush, QColor, QCursor, QFont, QFontDatabase, QIcon,
                         QLinearGradient, QPainter, QPen, QPixmap, QPolygonF)
from PyQt6.QtWidgets import QApplication, QPushButton, QSlider, QWidget

# ---- 常量 ----
SAMPLERATE = 48000
HOP = 256                                # 每帧滑动步长（重叠 93.75% → 更新率 ~188 Hz，输入块 5.3ms）
FPS = 60

# 频率物理边界（FFT 限制）
FREQ_ABS_MIN = 10.0
FREQ_ABS_MAX = SAMPLERATE / 2.0          # 24000

# 分层架构（2026-08-18，GPT 对齐）：实时线=短窗全频段（跟手），录制/精细=长窗低频（EQ 对比）
FFT_SIZE = 4096                          # 实时短窗：窗 85ms、组延迟 43ms，50Hz kick 也立刻动
FFT_FINE_SIZE = 65536                    # 录制精细长窗：bin 0.73Hz → 20~50Hz ~8.5px
FINE_CROSSOVER_HZ = 500.0                # 录制网格：<500 用长窗、>=500 用短窗
FINE_DIVISOR = 8                         # 长窗每 8 回调算一次（~23.5Hz，rfft(65536)~1ms 预算内）

# 非对称 Attack/Release（替代单 EMA）：上升快冲（kick 立即冲）、下降慢落（保留鼓点形状）
ATTACK = 0.80                            # target>current 时的新值权重（快攻）
RELEASE = 0.25                           # target<current 时的新值权重（慢落）

# dB 定义域：视口纵向滚动于 [-140, 0] dBFS；Ctrl+滚轮 y 轴缩放改变 10dB 单元格高度（=span）
DB_ABS_MIN = -140.0
DB_ABS_MAX = 0.0
DB_SPAN = 110.0                          # 默认视口跨度 = 可调最大值（10dB 单元格最高）
DB_SPAN_MIN = DB_SPAN                    # 缩放下限（默认即最大单元格，滚轮上无动作）
DB_SPAN_MAX = 140.0                      # 缩放上限（全范围 -140..0，10dB 单元格最矮）
DB_ZOOM_STEP = 10.0                      # Ctrl+滚轮每格跨度变化（dB）
DB_LO, DB_HI = -110.0, 0.0               # 默认视口（-110..0 dBFS，2026-08-18 调整）

# 频率定义域（固定，不横向缩放）
FREQ_MIN, FREQ_MAX = 20.0, 20000.0

# 布局（极致的无边框：刻度写进 plot 内部）
BORDER = 5
TOP_BAR = 30
MARGIN_TOP = 6
MARGIN_RIGHT = 6

BTN_STYLE = """
QPushButton {
    background: #1a0d12; color: #c9a3b1; border: 1px solid #2a141e;
    border-radius: 0px; font-size: 11px;
}
QPushButton:hover { background: #241018; }
QPushButton:pressed { background: #0d0609; }
"""

CLOSE_BTN_STYLE = """
QPushButton {
    background: #1a0d12; color: #c9a3b1; border: 1px solid #2a141e;
    border-radius: 0px; font-size: 11px;
}
QPushButton:hover { background: #d13438; color: #ffffff; border-color: #d13438; }
QPushButton:pressed { background: #a3262b; }
"""

# ---- 主题（皮肤）----
THEMES = {
    "sunset": {"bg": "#12090d", "line_bottom": "#4a1f30", "line_top": "#BD5075",
               "grid": "#2a141e", "text": "#8a5a6b", "border": "#1a0d12"},
}
THEME_ORDER = list(THEMES.keys())

# 包络轨道配色（多段异色对比，3 条够用）
TRACK_COLORS = ["#ff4d4d", "#ffd23f", "#3ddc84"]

# 内容透明度预设（O 循环档位；折线/坐标数字/dock 恒不透明；默认 25%）
OPACITY_PRESETS = [1.0, 0.70, 0.50, 0.25]

# 透明模式关（实底）时的内容透明度（dock/折线仍恒不透明）
OPACITY_SOLID = 0.85

# Hide 按钮：隐藏 dock 的时长（秒），期间悬停底部也不弹出
DOCK_HIDE_SECONDS = 10.0

# 模式联动（2026-08-20）：
#   top 开 → 置顶：trans 开 = 盖任务栏（贴全屏底）；trans 关 = 贴任务栏（跟随上沿）
#   top 关 → 强制预览 高度300 / tint80（与 trans 状态无关）；再开 top 恢复默认（100/8）
#   top 切换（无论开/关）→ trans 自动关闭（2026-08-20 优化）
MODE_OVERLAY_HEIGHT = 300          # top 关：强制高度
MODE_OVERLAY_TINT = 80             # top 关：强制 tint
DEFAULT_HEIGHT = 100               # 默认高度（恢复目标）
DEFAULT_TINT = 8                   # 默认 tint（恢复目标）
ADHERE_THRESHOLD = 250             # 贴任务栏模式：底边距目标超过此值 = 用户已拖走，不跟随
BOTTOM_GAP = 1                     # 窗口底边与锚点的 1px 间隙（保留频谱底线不贴死）
TOPMOST_REASSERT_INTERVAL = 0.15   # 盖任务栏模式：置顶重申周期（秒），防任务栏弹出/预览压过频谱（2026-08-22 0.5→0.15）
ADHERE_POLL_MS = 16                # 贴任务栏：检测/动画 tick（60Hz，任务栏动画 60Hz 已足够顺滑）
ADHERE_ANIM_S = 0.30               # 贴任务栏唤醒（弹出）动画时长（s），匹配任务栏唤醒动画（2026-08-22 调定）
ADHERE_ANIM_S_COLLAPSE = 0.40      # 贴任务栏收起（隐藏）动画时长（s）：任务栏隐藏动画明显更久（~0.4s）
ADHERE_END_OFFSET = 2              # 唤醒落点（贴任务栏）额外下移 px 贴平；收起落点保留 1px 给隐藏任务栏边线
ADHERE_WAKE_DELAY_S = 0.0          # 唤醒：无延迟，起步紧跟任务栏（2026-08-22）
ADHERE_COLLAPSE_DELAY_S = 0.1      # 收起：延迟 0.1s（任务栏隐藏动画更久，频谱滞后稍多）


# ---- 纯逻辑（可测试，无 PyQt / 音频副作用）----

def log_ticks(lo, hi):
    """对数刻度：每个 decade 内 1~9 全部刻度（含 3,4,6,7,8,9）。"""
    if lo <= 0 or hi <= lo:
        return []
    ticks = []
    for dec in range(math.floor(math.log10(lo)), math.ceil(math.log10(hi)) + 1):
        base = 10.0 ** dec
        for mult in range(1, 10):
            v = base * mult
            if lo - 1e-9 <= v <= hi + 1e-9:
                ticks.append(v)
    return ticks


def fine_freq_ticks(lo, hi):
    """10k~20k 更细刻度（每 1kHz），仅在最底部三格画短线。"""
    if lo <= 10000.0 and hi >= 20000.0:
        return [float(v) for v in range(11000, 20000, 1000)]
    return []


def lin_ticks(lo, hi):
    """线性刻度：自适应选择"好步长"，返回 [lo, hi] 内的刻度。"""
    span = hi - lo
    if span <= 0:
        return []
    step = 20000.0
    for s in (100.0, 200.0, 500.0, 1000.0, 2000.0, 5000.0, 10000.0, 20000.0):
        if span / s <= 12:
            step = s
            break
    ticks = []
    v = math.ceil(lo / step) * step
    while v <= hi + 1e-9:
        if v >= lo - 1e-9:
            ticks.append(v)
        v += step
    return ticks


def db_ticks(lo, hi, step=10.0):
    """返回 [lo, hi] 内以 step 为间隔的 dB 刻度（含边界）。"""
    ticks = []
    v = math.ceil(lo / step) * step
    while v <= hi + 1e-9:
        if v >= lo - 1e-9:
            ticks.append(v)
        v += step
    return ticks


def format_freq(v):
    """频率刻度文字（无 Hz 单位）：<1k 用数字，≥1k 用 k 后缀。"""
    if v >= 1000.0:
        return f"{v / 1000.0:g}k"
    return f"{int(round(v))}"


NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_frequency(midi):
    """MIDI 音高编号 → 频率（Hz），A4=440（MIDI 69）。"""
    return 440.0 * 2.0 ** ((midi - 69) / 12.0)


def note_name(midi):
    """MIDI 音高编号 → 音名（如 C4、C#4）。"""
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


# 音高标注覆盖全部显示频段：最高 MIDI 音 ≤ FREQ_MAX（20000 Hz → MIDI 135 ≈ 19.9 kHz，D#10）。
# 标准 MIDI 只到 G9 (127)，12.5k~20k 区间靠公式扩展出"没人用"的高音名，纯为铺满频谱图。
MIDI_MAX = int(math.floor(12.0 * math.log2(FREQ_MAX / 440.0) + 69.0))


# 透明边缘基准：频响曲线 1kHz 归零点所在的 dBFS 线（2026-08-18 由 -60 下调到 -70）
FR_REF_DB = -70.0
# y 轴显示单位偏移：'+70dBFS'（2026-08-18）——显示值 = dBFS+70，1kHz 频响基准线显示为 0
DB_DISPLAY_OFFSET = 70.0
GRID_REF_DB = -70.0                # 网格边界（2026-08-18 更正 -60→-70）：trans 开只画其下，关全画
NOTE_FR_OFFSET_PX = 15.0              # 音高文字中心距频响曲线的距离（px，2026-08-18 定）
HEIGHT_COMPACT = 200               # 窗口高度阈值：≤200 紧凑模式（无背景色/网格线，2026-08-18）
NOTE_NUM_CLAMP_DB = -40.0          # 音高音量数字显示下限（'+70dBFS' 单位 = -110dBFS）
NOTE_XAXIS_OVERLAP_PX = 10         # 紧凑模式：音量数字中心距底边 < 此值即与 x 轴音名重叠（让音名盖住音量）


def note_name_short(midi):
    """音名去八度（G#6 → G#）：紧凑模式 x 轴音名标注用。"""
    return NOTE_NAMES[midi % 12]


def freq_to_midi(freq):
    """频率 → 最近 MIDI 音高编号（A4=440 → 69）。"""
    return int(round(69.0 + 12.0 * math.log2(freq / 440.0)))


# 轴标注模式（Label 按钮循环）：频率数字 → 音名标注 → 最简（隐藏全部刻度数字）
LABEL_FREQ, LABEL_NOTES, LABEL_MIN = 0, 1, 2
LABEL_MODE_TEXT = ("Freq", "Note", "Min")


def db_display(db, clamp=NOTE_NUM_CLAMP_DB):
    """'+70dBFS' 显示值：dBFS + 70，低于 clamp 则钳制到 clamp（-40 = -110dBFS）。"""
    return max(db + DB_DISPLAY_OFFSET, clamp)


def floor_baseline(ys, bottom, floor_px=1.0):
    """把折线 y 坐标钳制到显示区底部 floor_px px 以内（音量不足时基线保留，不消失）。

    ys 为屏幕 y（向下增大）：超出 bottom-floor_px 的点拉回 floor 线，显示区内的点保持原样。
    np.minimum 方向关键——用 np.maximum 会把整条线压平到底线（2026-08-18 踩坑）。
    """
    return np.minimum(ys, bottom - floor_px)


# HD490 Pro 实测频响（oratory1990，mixing 耳罩，1kHz 归零）。
# 9~14kHz 的深谷是测量夹具（耳道耦合器）伪影，非耳机真实响应。
FR_POINTS = [
    (20.0, -4.31), (24.2, -3.41), (29.4, -2.64), (35.6, -2.07),
    (43.1, -1.65), (52.2, -1.58), (63.2, -1.72), (76.6, -1.74),
    (92.8, -1.86), (112.5, -2.07), (136.3, -2.38), (165.1, -2.58),
    (200.0, -2.72), (242.3, -2.67), (293.6, -2.49), (355.7, -2.30),
    (430.9, -1.98), (522.0, -1.57), (632.5, -1.46), (766.2, -1.23),
    (928.3, -0.33), (1124.7, 0.65), (1362.6, 1.52), (1650.8, 1.47),
    (2000.0, 1.53), (2423.1, 3.90), (2935.6, 4.76), (3556.6, 4.52),
    (4308.9, 5.75), (5220.3, 3.75), (6324.6, 2.82), (7662.4, 0.84),
    (9283.2, -8.04), (11246.8, -7.92), (13625.8, -10.58),
    (16508.1, -5.01), (20000.0, -8.43),
]


def headphone_fr(freq):
    """通用 HD490 Pro 参考频响（相对 dB），对数频率线性插值。"""
    fr_freq = np.array([p[0] for p in FR_POINTS], dtype=float)
    fr_db = np.array([p[1] for p in FR_POINTS], dtype=float)
    return np.interp(np.log10(freq), np.log10(fr_freq), fr_db)


def note_ref_db(freq):
    """音高文字锚点：频响曲线基准线（FR_REF_DB + 耳机相对频响），随曲线起伏。"""
    return FR_REF_DB + headphone_fr(freq)


def _mix_white(hex_color, t):
    """把 #rrggbb 颜色向白色混合 t（0~1）。"""
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    r = round(r + (255 - r) * t)
    g = round(g + (255 - g) * t)
    b = round(b + (255 - b) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def clamp_db(db_lo, db_hi, span=DB_SPAN):
    """把 dB 视口约束到 [-140, 0]，跨度固定为 span（默认 70）。"""
    db_hi = min(max(db_hi, DB_ABS_MIN + span), DB_ABS_MAX)
    db_lo = db_hi - span
    return db_lo, db_hi


def zoom_db_view(db_lo, db_hi, span, step, anchor_db=None):
    """Ctrl+滚轮 y 轴缩放：改 10dB 单元格高度（=span），返回 (db_lo, db_hi, span)。

    step>0（滚轮上）= 放大（span 减小，单元格变高）；step<0（滚轮下）= 缩小（span 增大，
    单元格变矮，直至全范围 0..140）。span 钳制到 [DB_SPAN_MIN, DB_SPAN_MAX]；
    以 anchor_db 为锚点重排视口（默认取视口中线）。
    """
    new_span = min(DB_SPAN_MAX, max(DB_SPAN_MIN, span - step * DB_ZOOM_STEP))
    if new_span == span:
        return db_lo, db_hi, span
    if anchor_db is None:
        anchor_db = (db_lo + db_hi) / 2.0
    lo, hi = clamp_db(anchor_db - new_span / 2.0, anchor_db + new_span / 2.0, new_span)
    return lo, hi, new_span


def mode_forced_preview(always_on_top):
    """top 关 → 强制预览模式（高度 300 / tint 80），与 trans 状态无关（2026-08-20）。
    切 top 会先自动关 trans（见 toggle_always_on_top），但规则本身不依赖 trans。"""
    return not always_on_top


def mode_anchor(transparent, always_on_top):
    """(trans, top) 组合 → 底边锚定策略（2026-08-20）：
    - above_taskbar：非置顶 → 贴任务栏上方（可用区底）
    - over_taskbar：trans 开 + top 开 → 盖任务栏（贴全屏底）
    - adhere：trans 关 + top 开 → 贴任务栏（弹出上移、收起下移）
    """
    if not always_on_top:
        return "above_taskbar"
    if transparent:
        return "over_taskbar"
    return "adhere"


def should_follow_taskbar(current_bottom, target, threshold=ADHERE_THRESHOLD):
    """贴任务栏模式：窗口底边距目标不超过阈值才跟随（防打扰用户手动拖走的窗口）。"""
    return abs(current_bottom - target) <= threshold


def ease_out_cubic(t):
    """缓动函数 easeOutCubic：t∈[0,1] → [0,1]（起步快、收尾缓——紧贴任务栏滑动的领先沿）。越界钳制。"""
    t = min(1.0, max(0.0, t))
    return 1.0 - (1.0 - t) ** 3


def adhere_step(y0, y1, t):
    """贴任务栏固定动画：t∈[0,1] 时刻的 easeOutCubic 插值 y（唤醒上移/收起下移，位移=任务栏高度）。"""
    return y0 + (y1 - y0) * ease_out_cubic(t)


def zorder_insert(always_on_top):
    """置顶/置底插入句柄：True → HWND_TOPMOST(-1)；False → HWND_BOTTOM(1)（真正最底，纯背景条）。"""
    return -1 if always_on_top else 1


def filter_sources(devices):
    """Dev 信号源筛选（2026-08-27）：只保留 耳机输出（立体声混音）与 麦克风输入，其余通道不入循环。

    - 耳机输出：优先含 'Realtek(R)' 的主混音，否则取第一个 立体声混音
    - 麦克风：优先含 'UGREEN' 的 USB 麦，否则取第一个 麦克风
    - 顺序固定：耳机输出 → 麦克风；每源只取 1 个设备
    """
    earphones, mics = [], []
    for i, d in enumerate(devices):
        if d["max_input_channels"] <= 0:
            continue
        name = d["name"]
        if "立体声混音" in name:
            earphones.append((i, d))
        elif "麦克风" in name:
            mics.append((i, d))
        # 其余全部忽略（Voicemeeter/CABLE/线路输入/realtek 2nd 等）

    def _first(candidates, prefer):
        for i, d in candidates:
            if prefer in d["name"]:
                return (i, d)
        return candidates[0] if candidates else None

    return [x for x in (_first(earphones, "Realtek(R)"), _first(mics, "UGREEN")) if x is not None]


def list_input_devices():
    """dev 切换目标输入设备：filter_sources(sd.query_devices()) 的薄壳。"""
    return filter_sources(sd.query_devices())


# ---- FFT 引擎（与 UI 分离）----

class SpectrumEngine:
    """双路径 FFT：实时线=短窗全频段（跟手）；录制/精细=长窗低频（EQ 对比，慢但细）。

    两条独立数据，互不污染：
    - 实时线（self.target/current）：4096 短窗，全频段，50Hz kick 也立即响应。
    - 录制网格（self.rec_target / current_max / tracks）：长窗 65536 低频(<500Hz) +
      短窗高频，max-hold 用于 EQ 预设对比。
    """

    def __init__(self, samplerate=SAMPLERATE):
        self.samplerate = samplerate
        self.hop = HOP
        self.gen = 0
        self.attack = ATTACK
        self.release = RELEASE
        self.cb_errors = 0          # 回调内异常计数（不外泄，仅记录）
        self.cb_last_error = ""

        # —— 实时线：短窗 FFT（全频段，跟手）——
        self.fft_size = FFT_SIZE
        self.window = np.hanning(FFT_SIZE)
        self.scale = 1.0 / (FFT_SIZE * self.window.mean())
        self.freqs = np.fft.rfftfreq(FFT_SIZE, 1.0 / samplerate)   # 2049 bins
        self.n_bins = len(self.freqs)
        with np.errstate(divide="ignore"):
            self.freqs_log10 = np.log10(self.freqs)
        self.buf = np.zeros(FFT_SIZE)
        self.mag = np.zeros(self.n_bins)
        self.target = np.full(self.n_bins, DB_ABS_MIN)
        self.current = np.full(self.n_bins, DB_ABS_MIN)

        # —— 录制/精细层：长窗低频 + 短窗高频 合并网格 ——
        self.fine_size = FFT_FINE_SIZE
        self.fine_window = np.hanning(FFT_FINE_SIZE)
        self.fine_scale = 1.0 / (FFT_FINE_SIZE * self.fine_window.mean())
        self.fine_freqs = np.fft.rfftfreq(FFT_FINE_SIZE, 1.0 / samplerate)
        self.fine_buf = np.zeros(FFT_FINE_SIZE)
        self.fine_gen = 0
        self.fine_updates = 0
        self.fine_mag = np.zeros(len(self.fine_freqs))
        self.fine_target = np.full(len(self.fine_freqs), DB_ABS_MIN)
        self.n_fine_low = int(np.searchsorted(self.fine_freqs, FINE_CROSSOVER_HZ))
        self.n_short_hi = int(np.searchsorted(self.freqs, FINE_CROSSOVER_HZ, side="right"))
        self.rec_freqs = np.concatenate([self.fine_freqs[:self.n_fine_low],
                                         self.freqs[self.n_short_hi:]])
        with np.errstate(divide="ignore"):
            self.rec_freqs_log10 = np.log10(self.rec_freqs)
        self.rec_n_bins = len(self.rec_freqs)
        self.rec_target = np.full(self.rec_n_bins, DB_ABS_MIN)

        self.recording = False
        self.current_max = np.full(self.rec_n_bins, DB_ABS_MIN)
        self.tracks = []                       # 已冻结的 max-hold 包络（录制网格）

    @staticmethod
    def _push(buf, mono, n, size):
        """环形缓冲：丢弃最旧 n 个样本，追加新样本（重叠 FFT）。"""
        if n >= size:
            buf[:] = mono[-size:]
        else:
            buf[:size - n] = buf[n:]
            buf[size - n:] = mono

    def audio_callback(self, indata, frames, time, status):
        """PortAudio 回调入口：任何异常都不得外泄到 CFFI（否则以 ~188Hz 刷屏弹窗）。"""
        try:
            self._audio_callback(indata, frames, time, status)
        except Exception:
            # 瞬时分配失败（如系统提交内存不足）等：跳过本帧，保留上一帧频谱，下一回调重试。
            # 只记录，不打印（noconsole 打包下 stderr 打印本身就是弹窗源）。
            import traceback
            self.cb_errors += 1
            self.cb_last_error = traceback.format_exc()

    def _audio_callback(self, indata, frames, time, status):
        self.gen += 1
        mono = indata.mean(axis=1)
        n = mono.shape[0]
        a, r = self.attack, self.release

        # —— 实时线（每回调）：短窗全频段，mag 用非对称 A/R ——
        self._push(self.buf, mono, n, self.fft_size)
        buf = self.buf - self.buf.mean()
        spec = np.abs(np.fft.rfft(buf * self.window)) * self.scale
        rising = spec >= self.mag
        k = np.where(rising, a, r)
        self.mag = self.mag * (1.0 - k) + spec * k
        self.target = np.clip(20.0 * np.log10(self.mag + 1e-9), DB_ABS_MIN, DB_ABS_MAX)

        # —— 录制/精细层：长窗低频（节流，max-hold 不需要实时）——
        self._push(self.fine_buf, mono, n, self.fine_size)
        self.fine_gen += 1
        if self.fine_gen >= FINE_DIVISOR:
            self.fine_gen = 0
            fbuf = self.fine_buf - self.fine_buf.mean()
            fspec = np.abs(np.fft.rfft(fbuf * self.fine_window)) * self.fine_scale
            self.fine_mag = fspec                     # 长窗本身已重平均，不需额外平滑
            self.fine_target = np.clip(20.0 * np.log10(self.fine_mag + 1e-9),
                                       DB_ABS_MIN, DB_ABS_MAX)
            self.fine_updates += 1
        # 录制合并 target（短窗高频每回调更新；长窗低频节流后更新）
        self.rec_target[:self.n_fine_low] = self.fine_target[:self.n_fine_low]
        self.rec_target[self.n_fine_low:] = self.target[self.n_short_hi:]
        if self.recording:
            # 录制 max-hold 只看前端快速层（engine.current），不叠加慢速 fine 背景层
            np.maximum(self.current_max, self.current, out=self.current_max)

    def start_recording(self):
        self.current_max = np.full(self.n_bins, DB_ABS_MIN)
        self.recording = True

    def stop_recording(self):
        if self.recording:
            self.tracks.append(self.current_max.copy())
            self.recording = False

    def clear_tracks(self):
        self.tracks = []
        self.recording = False              # 清空同时停止正在进行的录制
        self.current_max = np.full(self.n_bins, DB_ABS_MIN)


# ---- 无边框窗口 ----

class SpectrumWindow(QWidget):
    def __init__(self, engine, config_path=None, device=None):
        super().__init__()
        self.engine = engine
        self.config_path = config_path

        self.theme_name = THEME_ORDER[0]
        self.theme = dict(THEMES[self.theme_name])
        self.tint = DEFAULT_TINT
        self.always_on_top = True    # 默认置顶（2026-08-18）
        self.transparent = True
        self.show_border = False             # 左/下/右边框显示开关（工具栏 Br 切换）；默认无边框
        self.opacity = 0.25                 # 内容透明度（QPainter 逐像素；折线/坐标数字/dock 恒不透明；默认 25%）
        self._preview_forced = False         # top 关 的强制预览模式是否生效中（高度300/tint80，与 trans 无关）
        self._positioned = False             # 窗口是否已定位过（首次定位水平居中，之后保留用户 x）
        self._last_topmost_assert = 0.0      # 上次重申置顶时间（盖任务栏模式防任务栏压过）
        self._adhere_last_top = None         # 贴任务栏：上次轮询的任务栏上沿 y（运动检测，None=未初始化）
        self._adhere_anim = None             # 贴任务栏：进行中的固定动画 {y0, y1, t0, dur}

        self.freq_lo, self.freq_hi = FREQ_MIN, FREQ_MAX
        self.db_lo, self.db_hi = DB_LO, DB_HI
        self.db_span = DB_SPAN
        self.log_scale = True

        self.paused = False
        self.fps = FPS

        self.input_devices = list_input_devices()
        self.dev_pos = self._resolve_device(device)
        self.stream = None

        self.axis_font = _pick_font(8)
        self.note_font = _pick_font(8)
        self.label_mode = LABEL_FREQ   # 轴标注模式：Freq → Note → Min 循环

        # 渲染缓存（按 plot 状态 × 频率网格 键控）+ 曲线更新率计数
        self._proj_cache = {}
        self._static_bg = None
        self._static_fg = None
        self._static_key_cached = None
        self._updates = 0
        self._last_gen = 0
        self._upd_start = time.monotonic()
        self.update_rate = 0.0

        self.setWindowTitle("Spectrum")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        # 100px 高对应默认 110 dB 视口（每 10 dB ≈ 9.1px，单元格最大）；Ctrl+滚轮可调 span 110~140
        self.resize(2560, DEFAULT_HEIGHT)
        self._build_toolbar()
        self._apply_mode_rules()      # 初始定位按模式锚定（trans 开 + top 开 → 底边贴全屏底盖任务栏）

        # 点击穿透：除 dock（工具条）区外全部 click-through，不挡下层窗口。
        # 30ms 定时器轮询光标 → dock 区移除 WS_EX_TRANSPARENT（可交互），否则加上（穿透）。
        self._click_through = False
        self._dock_hidden_until = 0.0   # Hide 按钮：此时间点前 dock 保持隐藏
        self._dock_hidden_forever = False  # Hide 右键：本次会话 dock 永不再弹出（直至下次启动）
        self._click_timer = QTimer(self)
        self._click_timer.setInterval(30)
        self._click_timer.timeout.connect(self._update_click_through)
        self._click_timer.start()

        # 贴任务栏（trans 关 + top 开）：60Hz 检测任务栏上沿滑动运动 → 播固定时长滑动动画
        self._adhere_timer = QTimer(self)
        self._adhere_timer.setInterval(ADHERE_POLL_MS)
        self._adhere_timer.timeout.connect(self._track_taskbar)
        self._adhere_timer.start()

    # ---- 设备 ----
    def _resolve_device(self, device_arg):
        if not self.input_devices:
            return None
        if device_arg is not None:
            for pos, (idx, _d) in enumerate(self.input_devices):
                if idx == device_arg:
                    return pos
        for pos, (idx, d) in enumerate(self.input_devices):
            if "立体声混音" in d["name"]:
                return pos
        return 0

    def _open_stream(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        if not self.input_devices or self.dev_pos is None:
            return
        dev_idx, info = self.input_devices[self.dev_pos]
        channels = min(2, info["max_input_channels"])
        self.stream = sd.InputStream(
            device=dev_idx, channels=channels,
            samplerate=SAMPLERATE, blocksize=HOP,
            callback=self.engine.audio_callback)
        self.stream.start()

    def shutdown(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    # ---- (trans, top) 模式联动 ----
    def _apply_mode_rules(self):
        """(trans, top) 模式联动规则（2026-08-20）：

        - top 开：置顶 —— trans 开 = 盖任务栏（底边贴全屏底）；trans 关 = 贴任务栏（跟随上沿）
        - top 关：强制预览 —— 高度=300 / tint=80（与 trans 状态无关）；再开 top 恢复默认（100/8）
        - Top 切换（开或关）→ trans 自动关闭（见 toggle_always_on_top）
        """
        forced = mode_forced_preview(self.always_on_top)
        if forced and not self._preview_forced:
            self._preview_forced = True
            self.set_window_height(MODE_OVERLAY_HEIGHT)
            self.set_tint(MODE_OVERLAY_TINT)
            self.height_slider.setValue(MODE_OVERLAY_HEIGHT)
            self.tint_slider.setValue(MODE_OVERLAY_TINT)
        elif not forced and self._preview_forced:
            self._preview_forced = False
            self.set_window_height(DEFAULT_HEIGHT)
            self.set_tint(DEFAULT_TINT)
            self.height_slider.setValue(DEFAULT_HEIGHT)
            self.tint_slider.setValue(DEFAULT_TINT)
        self._reposition_for_mode()

    def _reposition_for_mode(self):
        """按 (trans, top) 重新锚定底边（首次定位水平居中，之后保留用户 x 坐标）。"""
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        if not self._positioned:
            x = screen.geometry().x() + (screen.geometry().width() - self.width()) // 2
            self._positioned = True
        else:
            x = self.x()
        anchor = mode_anchor(self.transparent, self.always_on_top)
        if anchor == "above_taskbar":
            # 非置顶：底边贴回任务栏上方（可用区底），留 1px 底线，不挡任务栏
            geo = screen.availableGeometry()
            self.move(x, geo.y() + geo.height() - self.height() - BOTTOM_GAP)
        elif anchor == "over_taskbar":
            # 盖任务栏：底边贴全屏底（含任务栏区域），留 1px 底线
            geo = screen.geometry()
            self.move(x, geo.y() + geo.height() - self.height() - BOTTOM_GAP)
        else:
            # 贴任务栏上沿（弹出/收起由 _track_taskbar 持续跟随）
            self._snap_to_taskbar(force=True)

    def _taskbar_geometry(self):
        """任务栏当前几何。返回 (rect_top, height, screen_bottom)：
        - rect_top：任务栏上沿 y（全屏坐标；自动隐藏收起时 = 屏底，rect 在屏外）
        - height：任务栏高度（rect 恒有，滑动中不变）
        - screen_bottom：主屏底部 y
        """
        screen = QApplication.primaryScreen()
        if screen is None:
            return self.geometry().bottom(), 40, self.geometry().bottom()   # 兜底：保持原位
        screen_bottom = screen.geometry().bottom()
        hwnd = user32.FindWindowW("Shell_TrayWnd", None)
        if hwnd:
            rect = wintypes.RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(rect)) and rect.bottom > rect.top:
                return rect.top, rect.bottom - rect.top, screen_bottom
        return screen_bottom, 40, screen_bottom

    @staticmethod
    def _adhere_anchor(top, height, screen_bottom):
        """贴任务栏锚点（窗口底边应贴到的 y）：顶边在屏内=任务栏上沿（最终位），否则=屏底。
        唤醒落点（贴任务栏）下移 ADHERE_END_OFFSET 贴平任务栏；收起落点保留 1px 留白
        （隐藏任务栏仍露出 1px 边线，不能被频谱盖住）。"""
        if top < screen_bottom:
            return screen_bottom - height - BOTTOM_GAP + ADHERE_END_OFFSET
        return screen_bottom - BOTTOM_GAP

    def _snap_to_taskbar(self, force=False):
        """窗口底边贴到任务栏当前锚点（任务栏自动隐藏收起时 = 全屏底）。
        force 用于模式切换强制定位；同步贴任务栏运动检测的基准 y。"""
        top, height, screen_bottom = self._taskbar_geometry()
        anchor = self._adhere_anchor(top, height, screen_bottom)
        if not force and not should_follow_taskbar(self.geometry().bottom(), anchor):
            return    # 用户已把窗口拖离底部，不打扰
        self.move(self.x(), anchor - self.height())
        self._adhere_last_top = top

    def _reassert_topmost(self):
        """盖任务栏模式：周期性重申 HWND_TOPMOST，防任务栏弹出时压过频谱（节流避免高频调用）。"""
        now = time.monotonic()
        if now - self._last_topmost_assert < TOPMOST_REASSERT_INTERVAL:
            return
        self._last_topmost_assert = now
        self._apply_topmost()

    def _track_adhere(self):
        """贴任务栏状态机（60Hz）：检测任务栏上沿滑动运动 → 立即播固定时长滑动动画。

        任务栏唤醒/收起是固定速度/时间/位移的**滑动**（唤醒 ~0.3s、收起 ~0.4s）。旧检测卡在
        "底边未完全出屏"（bottom<=屏底+1）才翻状态 → 唤醒要等滑动到末段才触发 → 频谱落后
        ~0.2s。改为直接看任务栏**上沿 y 的位移**——上移=唤醒（时长 ADHERE_ANIM_S、无延迟）、
        下移=收起（时长 ADHERE_ANIM_S_COLLAPSE、延迟 ADHERE_COLLAPSE_DELAY_S），滑动第一帧即
        触发；动画目标=最终锚点（位移=任务栏高度），不追中间态。系统动画关（rect 跳变）同样瞬时响应。
        """
        top, height, screen_bottom = self._taskbar_geometry()
        now = time.monotonic()

        anim = self._adhere_anim
        if anim is not None:
            t = (now - anim["t0"]) / anim["dur"]
            if t >= 1.0:
                self.move(self.x(), int(anim["y1"]))
                self._adhere_anim = None
            else:
                self.move(self.x(), int(round(adhere_step(anim["y0"], anim["y1"], t))))
            self._adhere_last_top = top
            return

        if self._adhere_last_top is None:
            self._adhere_last_top = top
            return

        delta = top - self._adhere_last_top
        self._adhere_last_top = top
        if abs(delta) < 1.0:
            return                        # 任务栏静止：不打扰

        if delta < 0:                     # 上沿上移 = 唤醒 → 目标=任务栏最终上沿（下移贴平任务栏）
            anchor = screen_bottom - height - BOTTOM_GAP + ADHERE_END_OFFSET
            dur, delay = ADHERE_ANIM_S, ADHERE_WAKE_DELAY_S
        else:                             # 上沿下移 = 收起 → 目标=屏底（保留 1px 给隐藏任务栏边线）
            anchor = screen_bottom - BOTTOM_GAP
            dur, delay = ADHERE_ANIM_S_COLLAPSE, ADHERE_COLLAPSE_DELAY_S
        if should_follow_taskbar(self.geometry().bottom(), anchor):
            self._adhere_anim = {"y0": float(self.y()), "y1": float(anchor - self.height()),
                                 "t0": now + delay, "dur": dur}

    def _track_taskbar(self):
        """60Hz 轮询：
        - trans 关 + top 开：贴任务栏 —— 检测上沿滑动运动 → 固定动画（见 _track_adhere）
        - trans 开 + top 开：盖任务栏，周期性重申置顶防任务栏压过
        """
        if not self.always_on_top:
            return
        if self.transparent:
            self._reassert_topmost()
        else:
            self._track_adhere()

    # ---- 主题 ----
    def set_theme(self, name):
        key = name.strip().lower()
        if key in THEMES:
            self.theme_name = key
            self.theme = dict(THEMES[key])
            self._apply_slider_style()

    # ---- 工具条动作 ----
    def cycle_device(self):
        if not self.input_devices:
            return
        self.dev_pos = (self.dev_pos + 1) % len(self.input_devices)
        self._open_stream()

    def toggle_pause(self):
        self.paused = not self.paused
        self.btn_pause.setText("▶" if self.paused else "⏸")

    def reset_view(self):
        self.freq_lo, self.freq_hi = FREQ_MIN, FREQ_MAX
        self.db_span = DB_SPAN
        self.db_lo, self.db_hi = DB_LO, DB_HI
        self.update()

    def reload_config(self):
        if self.config_path:
            load_config(self, self.config_path)
            self.update()

    def toggle_recording(self):
        if self.engine.recording:
            self.engine.stop_recording()
            self.btn_rec.setText("Rec")
        else:
            self.engine.start_recording()
            self.btn_rec.setText("Stop")
        self.update()

    def clear_tracks(self):
        self.engine.clear_tracks()
        self.btn_rec.setText("Rec")   # 清空同时停止录制，按钮复位
        self.update()

    def toggle_scale(self):
        self.log_scale = not self.log_scale
        self.btn_scale.setText("Log" if self.log_scale else "Lin")
        self.update()

    def cycle_label_mode(self):
        """Label 按钮 / N 键：轴标注三模式循环 Freq → Note → Min。

        - Freq：x 轴频率数字 + y 轴 dB 数字（默认）；
        - Note：音名标注（详情模式垂直音名+音量数字；紧凑模式 x 轴音名替换频率数字）；
        - Min：最简 —— 隐藏全部刻度数字（x/y 轴都不画），只留频谱与网格。
        """
        self.label_mode = (self.label_mode + 1) % 3
        self.btn_label.setText(LABEL_MODE_TEXT[self.label_mode])
        self.update()

    def set_tint(self, value):
        self.tint = int(value)
        self.update()

    def toggle_transparent(self):
        """Trans：透明模式开关（手动）。
        关（实底）→ 内容透明度切到 OPACITY_SOLID(85%)；开 → 恢复之前的值（默认 25%）。
        dock/折线/坐标数字恒不透明，不受内容透明度影响。
        联动（2026-08-20）：trans 变化会重锚底边（盖任务栏 ↔ 贴任务栏）并触发强制模式。
        """
        self._set_transparent(not self.transparent)

    def _set_transparent(self, on):
        """设置 Trans 状态（非翻转）。手动按钮与 Top 切换自动关 trans 共用（2026-08-20）。"""
        if on == self.transparent:
            return
        self.transparent = on
        if not on:
            self._opacity_saved = self.opacity
            self.opacity = OPACITY_SOLID
        else:
            self.opacity = getattr(self, "_opacity_saved", self.opacity)
        self._apply_mode_rules()
        self.update()

    def toggle_border(self):
        """工具栏 Br：切换左/下/右边框显示。"""
        self.show_border = not self.show_border
        self.update()

    def cycle_opacity(self):
        """热键 O：循环内容透明度预设（100/70/50/25%；折线/坐标数字/dock 恒不透明）。"""
        i = OPACITY_PRESETS.index(self.opacity) if self.opacity in OPACITY_PRESETS else 0
        self.opacity = OPACITY_PRESETS[(i + 1) % len(OPACITY_PRESETS)]
        self.update()

    def hide_dock(self):
        """Hide 按钮（左键）：隐藏 dock DOCK_HIDE_SECONDS 秒，期间悬停底部也不弹出。"""
        self._dock_hidden_until = time.monotonic() + DOCK_HIDE_SECONDS
        self._set_toolbar_visible(False)
        self._set_click_through(True)

    def hide_dock_forever(self):
        """Hide 按钮（右键）：隐藏 dock 直至下次启动（本次会话永不再弹出，悬停底部也不弹出）。"""
        self._dock_hidden_forever = True
        self._set_toolbar_visible(False)
        self._set_click_through(True)

    def toggle_always_on_top(self):
        """Top：置顶开关。
        联动（2026-08-20 优化）：无论开/关，切 top 时 trans 自动关闭（转实底）；
        top 关 → 强制预览 高度300 / tint80（与 trans 无关），top 开 → 恢复默认 100/8。
        启动不触发（保持默认 trans 开 + top 开 = 盖任务栏）。
        """
        self.always_on_top = not self.always_on_top
        if self.transparent:
            self._set_transparent(False)   # 内部会 _apply_mode_rules + update
        else:
            self._apply_mode_rules()       # 联动：强制预览/恢复默认高度与tint
        self._apply_topmost()

    def _apply_topmost(self):
        """应用置顶/置底（SetWindowPos 无闪烁切换；setWindowFlag 会重建原生窗口闪烁几帧）。
        top 开 → HWND_TOPMOST；top 关 → HWND_BOTTOM 真正最底（2026-08-22，压到所有窗口之下）。"""
        hwnd = int(self.winId())
        user32.SetWindowPos(hwnd, zorder_insert(self.always_on_top), 0, 0, 0, 0,
                            0x2 | 0x1 | 0x10)        # SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE

    def advance(self):
        if not self.paused:
            # 非对称 Attack/Release：上升快冲（kick 立即冲）、下降慢落（保留鼓点形状）
            eng = self.engine
            rising = eng.target >= eng.current
            eng.current += np.where(rising, eng.attack, eng.release) * (eng.target - eng.current)
        gen = self.engine.gen
        if gen != self._last_gen:
            self._updates += gen - self._last_gen
            self._last_gen = gen
        now = time.monotonic()
        if now - self._upd_start >= 0.5:
            self.update_rate = self._updates / (now - self._upd_start)
            self._updates = 0
            self._upd_start = now
        self.update()

    # ---- 垂直滚动 / 缩放 dB 定义域 ----
    def _pan_v(self, step):
        self.db_hi += step * 6.0
        self.db_lo, self.db_hi = clamp_db(self.db_lo, self.db_hi, self.db_span)
        self._layout_toolbar()

    def _zoom_v(self, step, pos):
        """Ctrl+滚轮：y 轴缩放（改 10dB 单元格高度）。默认=最大单元格 → 滚轮上在 70 处无动作。"""
        plot = self._plot_rect()
        anchor_db = None
        if plot.contains(pos):
            frac = (plot.bottom() - pos.y()) / plot.height()
            anchor_db = self.db_lo + frac * (self.db_hi - self.db_lo)
        self.db_lo, self.db_hi, self.db_span = zoom_db_view(
            self.db_lo, self.db_hi, self.db_span, step, anchor_db)
        self._layout_toolbar()

    # ---- 工具条（自动隐藏）----
    def _build_toolbar(self):
        self.btn_close = self._make_btn("×", 24, self.close, style=CLOSE_BTN_STYLE)
        self.btn_rec = self._make_btn("Rec", 48, self.toggle_recording)
        self.btn_clear = self._make_btn("Clear", 48, self.clear_tracks)
        self.btn_dev = self._make_btn("Dev", 38, self.cycle_device)
        self.btn_pause = self._make_btn("⏸", 28, self.toggle_pause)
        self.btn_dbrange = self._make_btn("dB", 30, self.reset_view)
        self.btn_scale = self._make_btn("Log", 40, self.toggle_scale)
        self.btn_label = self._make_btn("Freq", 40, self.cycle_label_mode)
        self.btn_label.setToolTip("轴标注循环：Freq 频率数字 → Note 音名 → Min 隐藏全部数字")
        self.btn_trans = self._make_btn("Trans", 46, self.toggle_transparent)
        self.btn_top = self._make_btn("Top", 40, self.toggle_always_on_top)
        self.btn_border = self._make_btn("Br", 32, self.toggle_border)
        self.btn_hide = self._make_btn("Hide", 36, self.hide_dock)
        self.btn_hide.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.btn_hide.customContextMenuRequested.connect(self.hide_dock_forever)
        self.btn_hide.setToolTip("左键隐藏 10s / 右键隐藏至下次启动")
        self.btn_reload = self._make_btn("⟳", 28, self.reload_config)
        self.tint_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.tint_slider.setRange(0, 100)
        self.tint_slider.setValue(self.tint)
        self.tint_slider.setFixedSize(120, 20)
        self.tint_slider.setToolTip("tint")
        self.tint_slider.valueChanged.connect(self.set_tint)
        self.height_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.height_slider.setRange(100, 400)
        self.height_slider.setValue(self.height())
        self.height_slider.setFixedSize(90, 20)
        self.height_slider.setToolTip("窗口高度 100-400px（≤200 紧凑模式）")
        self.height_slider.valueChanged.connect(self.set_window_height)
        self.toolbar_btns = [self.btn_rec, self.btn_clear, self.btn_dev,
                             self.btn_pause, self.btn_dbrange, self.btn_scale,
                             self.btn_label, self.btn_trans, self.btn_top,
                             self.btn_border, self.btn_hide, self.btn_reload]
        self._apply_slider_style()
        self._toolbar_visible = False
        self._set_toolbar_visible(False)

    def set_window_height(self, value):
        """dock 高度 slider：改窗口高度，底边锚定不动。

        单次 setGeometry 同时改位置与高度（y+height 不变）——避免 resize→move
        两步中间态导致的 dock 抖动 / slider 逃离鼠标。
        """
        h = int(value)
        if h == self.height():
            return
        self.setGeometry(self.x(), self.y() + self.height() - h, self.width(), h)

    def _make_btn(self, text, width, slot, style=BTN_STYLE):
        btn = QPushButton(text, self)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setFixedSize(width, 20)
        btn.setStyleSheet(style)
        btn.clicked.connect(slot)
        return btn

    def _apply_slider_style(self):
        """滑块 knob/groove 跟随主题色（line_top 高亮），非默认蓝。"""
        accent = self.theme["line_top"]
        groove = self.theme["grid"]
        bg = self.theme["bg"]
        style = f"""
QSlider::groove:horizontal {{
    border: 1px solid {groove};
    height: 4px;
    background: {bg};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {accent};
    width: 10px;
    margin: -4px 0;
    border-radius: 5px;
}}
"""
        for s in (self.tint_slider, self.height_slider):
            s.setStyleSheet(style)

    def _set_toolbar_visible(self, visible):
        self._toolbar_visible = visible
        for btn in self.toolbar_btns:
            btn.setVisible(visible)
        self.tint_slider.setVisible(visible)
        self.height_slider.setVisible(visible)
        self.btn_close.setVisible(visible)
        self.update()

    # ---- 点击穿透（除 dock 外全部穿透）----
    def _update_click_through(self):
        """轮询光标：dock 区内可交互（移除 WS_EX_TRANSPARENT + 显示工具条），否则穿透。"""
        if self._dock_hidden_forever or time.monotonic() < self._dock_hidden_until:
            # Hide 生效中（10s 或 至下次启动）：dock 保持隐藏，悬停底部也不弹出
            self._set_toolbar_visible(False)
            self._set_click_through(True)
            return
        pos = self.mapFromGlobal(QCursor.pos())
        in_dock = self._in_toolbar_zone(pos)
        if in_dock:
            self._set_toolbar_visible(True)
            self._set_click_through(False)
        else:
            self._set_toolbar_visible(False)
            self._set_click_through(True)

    def _set_click_through(self, enabled):
        if enabled == self._click_through:
            return
        self._click_through = enabled
        try:
            hwnd = int(self.winId())
            style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            if enabled:
                style |= WS_EX_TRANSPARENT
            else:
                style &= ~WS_EX_TRANSPARENT
            user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
        except Exception:
            pass   # 无真实窗口句柄（离屏测试等）时忽略

    def _layout_toolbar(self):
        y = self._dock_y()
        items = list(self.toolbar_btns) + [self.tint_slider, self.height_slider,
                                           self.btn_close]
        total = sum(w.width() for w in items) + 6 * (len(items) - 1)
        x = (self.width() - total) // 2
        for btn in self.toolbar_btns:
            btn.move(x, y)
            x += btn.width() + 6
        self.tint_slider.move(x, y)
        x += self.tint_slider.width() + 6
        self.height_slider.move(x, y)
        x += self.height_slider.width() + 6
        self.btn_close.move(x, y)
        self._close_x = x

    def _dock_y(self):
        return self.height() - BORDER - 24

    def _in_toolbar_zone(self, pos):
        if pos.y() < self.height() - 60:
            return False
        return abs(pos.x() - self.width() / 2.0) <= 500

    # ---- 事件 ----
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        # 工具条显隐由点击穿透定时器驱动（click-through 时收不到事件）
        if e.buttons() & Qt.MouseButton.LeftButton and hasattr(self, "_drag_offset"):
            self.move(e.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(e)

    def leaveEvent(self, e):
        super().leaveEvent(e)

    def wheelEvent(self, e):
        delta = e.angleDelta().y()
        if delta == 0:
            return
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._zoom_v(delta / 120.0, e.position())
        else:
            self._pan_v(delta / 120.0)
        self.update()
        e.accept()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.close()
        elif e.key() == Qt.Key.Key_R:
            self.toggle_recording()
        elif e.key() == Qt.Key.Key_C:
            self.clear_tracks()
        elif e.key() == Qt.Key.Key_L:
            self.toggle_scale()
        elif e.key() == Qt.Key.Key_N:
            self.cycle_label_mode()
        elif e.key() == Qt.Key.Key_O:
            self.cycle_opacity()
        else:
            super().keyPressEvent(e)

    def resizeEvent(self, e):
        self._layout_toolbar()
        super().resizeEvent(e)

    # ---- 绘制 ----
    def paintEvent(self, event):
        key = self._static_key()
        if self._static_bg is None or self._static_key_cached != key:
            self._rebuild_static()
            self._static_key_cached = key

        p = QPainter(self)
        p.setOpacity(self.opacity)   # 内容透明度（QPainter 逐像素；折线/坐标数字/dock 豁免）
        p.drawPixmap(0, 0, self._static_bg)

        plot = self._plot_rect()
        if plot.width() > 0 and plot.height() > 0:
            self._draw_spectrum(p, plot)
            self._draw_fine_ref(p, plot)
            if self.engine.recording:
                color = TRACK_COLORS[len(self.engine.tracks) % len(TRACK_COLORS)]
                self._draw_envelope(p, plot, self.engine.current_max, color, dashed=True)
            if self.label_mode == LABEL_NOTES:
                self._draw_note_markers(p, plot)
                # 紧凑模式：底部音名替代跟随曲线的音名（见 _draw_x_axis_notes）
                if self.height() > HEIGHT_COMPACT:
                    self._draw_notes(p, plot)

        p.setOpacity(1.0)            # 坐标刻度数字恒不透明
        p.drawPixmap(0, 0, self._static_axis)
        p.setOpacity(self.opacity)   # 包络/图例/边框按原生透明度
        p.drawPixmap(0, 0, self._static_soft)

        p.setOpacity(1.0)            # dock 恒不透明（工具条背景 + 按钮 + 状态指示）
        if self._toolbar_visible:
            dy = self._dock_y()
            cx = self.width() / 2.0
            p.fillRect(QRectF(cx - 500, dy - 4, 1000, 30), QColor(18, 9, 13, 255))
        if self.engine.recording:
            self._draw_rec_indicator(p)
        if self._toolbar_visible:
            self._draw_fps(p)
        p.end()

    def _plot_rect(self):
        # 有边框：内容内缩 BORDER；无边框（Br 关）：内容占满窗口边缘（真无边框）
        m = BORDER if self.show_border else 0
        x = m
        y = m
        w = self.width() - 2 * m
        h = self.height() - 2 * m
        return QRectF(x, y, max(0, w), max(0, h))

    def _freq_to_x(self, freq, plot):
        # np.log10 对标量与数组都安全（_draw_fr_region 传 numpy 数组进来）
        if self.log_scale:
            frac = ((np.log10(freq) - np.log10(self.freq_lo)) /
                    (np.log10(self.freq_hi) - np.log10(self.freq_lo)))
        else:
            frac = (freq - self.freq_lo) / (self.freq_hi - self.freq_lo)
        return plot.left() + frac * plot.width()

    def _db_to_y(self, db, plot):
        frac = (db - self.db_lo) / (self.db_hi - self.db_lo)
        return plot.bottom() - frac * plot.height()

    def _draw_grid(self, p, plot):
        p.setPen(QPen(QColor(self.theme["grid"]), 1))
        # 纵向频率线：trans 开只画 GRID_REF_DB 以下（透明区无网格），关则全高
        y_top = plot.top() if not self.transparent else self._db_to_y(GRID_REF_DB, plot)
        ticks = log_ticks(self.freq_lo, self.freq_hi) if self.log_scale \
            else lin_ticks(self.freq_lo, self.freq_hi)
        for v in ticks:
            x = self._freq_to_x(v, plot)
            p.drawLine(QPointF(x, y_top), QPointF(x, plot.bottom()))
        # 10k~20k 细刻度：仅最底部三格画短线
        if self.log_scale:
            y_fine_top = self._db_to_y(-90.0, plot)
            for v in fine_freq_ticks(self.freq_lo, self.freq_hi):
                x = self._freq_to_x(v, plot)
                p.drawLine(QPointF(x, y_fine_top), QPointF(x, plot.bottom()))
        # 横向 dB 线：trans 开只画 GRID_REF_DB 以下；关则全画（含 -60 以上）
        for v in db_ticks(self.db_lo, self.db_hi):
            if self.transparent and v > GRID_REF_DB:
                continue
            y = self._db_to_y(v, plot)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

    def _projection(self, plot, freqs=None, freqs_log10=None):
        """缓存：可见 bin 范围 + 每 bin 的 x 位置（按 plot 状态 × 频率网格键控）。

        freqs=None 时用实时网格（engine.freqs）；传录制网格则用其 log10。
        """
        if freqs is None:
            freqs = self.engine.freqs
            freqs_log10 = self.engine.freqs_log10
        key = (plot.left(), plot.top(), plot.width(), plot.height(),
               self.freq_lo, self.freq_hi, self.log_scale, id(freqs))
        cached = self._proj_cache.get(key)
        if cached is not None:
            return cached
        lo = max(1, int(np.searchsorted(freqs, self.freq_lo)) - 1)
        hi = min(len(freqs), int(np.searchsorted(freqs, self.freq_hi, side="right")) + 1)
        if self.log_scale:
            log_lo = math.log10(self.freq_lo)
            log_range = math.log10(self.freq_hi) - log_lo
            xs_full = plot.left() + (freqs_log10[lo:hi] - log_lo) / log_range * plot.width()
        else:
            xs_full = plot.left() + (freqs[lo:hi] - self.freq_lo) / (self.freq_hi - self.freq_lo) * plot.width()
        self._proj_cache[key] = (key, lo, hi, xs_full)
        return self._proj_cache[key]

    def _bin_polygon(self, db_array, plot, freqs=None, freqs_log10=None):
        """把 dB 数组映射为 plot 内的折线点（全频段 2px 一列取 max）。

        freqs/freqs_log10=None 时用实时网格；录制包络传 engine.rec_freqs 网格。
        """
        _key, lo, hi, xs_full = self._projection(plot, freqs, freqs_log10)
        if hi - lo < 2:
            return []
        v = db_array[lo:hi]
        frac = np.clip((v - self.db_lo) / (self.db_hi - self.db_lo), 0.0, 1.0)
        xs = xs_full
        x_4k = self._freq_to_x(4000.0, plot)
        low = xs < x_4k
        xs_low, fr_low = self._reduce_cols(xs[low], frac[low], plot, 2.0)
        xs_high, fr_high = self._reduce_cols(xs[~low], frac[~low], plot, 2.0)
        xs_out = np.concatenate([xs_low, xs_high])
        fr_out = np.concatenate([fr_low, fr_high])
        order = np.argsort(xs_out)
        xs_out = xs_out[order]
        fr_out = fr_out[order]
        # 信号低于可视区（音量不足）时，底线保留在显示区底部 1px 内，曲线不消失
        ys_out = floor_baseline(plot.bottom() - fr_out * plot.height(), plot.bottom())
        return [QPointF(float(x), float(y)) for x, y in zip(xs_out, ys_out)]

    def _reduce_cols(self, xs, frac, plot, col_w):
        """按 col_w 像素一列归并 (xs, frac)，列内取 max 保峰。"""
        if len(xs) == 0:
            return np.array([]), np.array([])
        ncols = int(plot.width() / col_w) + 1
        col = np.clip(np.floor((xs - plot.left()) / col_w).astype(np.intp), 0, ncols - 1)
        fr_cols = np.full(ncols, -1.0)
        np.maximum.at(fr_cols, col, frac)
        mask = fr_cols >= 0.0
        return plot.left() + np.arange(ncols)[mask] * col_w + col_w / 2.0, fr_cols[mask]

    def _draw_fine_ref(self, p, plot):
        """精细参考线：长窗低频频谱（20-500Hz）叠加在实时主线之上，显示低频真实频率结构。

        亮粉色 + 50% 透明度：存在感强但不完全覆盖主线；慢动态，不参与 kick 跟手。
        """
        n = self.engine.n_fine_low
        if n < 2:
            return
        freqs = self.engine.rec_freqs[:n]
        flog = self.engine.rec_freqs_log10[:n]
        pts = self._bin_polygon(self.engine.rec_target[:n], plot, freqs, flog)
        if len(pts) < 2:
            return
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(240, 150, 185, 128), 1.5))
        p.drawPolyline(QPolygonF(pts))

    def _draw_spectrum(self, p, plot):
        pts = self._bin_polygon(self.engine.current, plot)
        if len(pts) < 2:
            return

        # 渐变填充（曲线下方）：白色 tint 只作用于这个色块；跟随原生透明度
        t = self.tint / 100.0
        fill = QPolygonF(pts)
        fill.append(QPointF(plot.right(), plot.bottom()))
        fill.append(QPointF(plot.left(), plot.bottom()))
        grad = QLinearGradient(0, plot.bottom(), 0, plot.top())
        grad.setColorAt(0.0, QColor(_mix_white(self.theme["line_bottom"], t)))
        grad.setColorAt(1.0, QColor(_mix_white(self.theme["line_top"], t)))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawPolygon(fill)

        # 折线：恒不透明（豁免原生透明度；不受 tint 影响）
        p.save()
        p.setOpacity(1.0)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(self.theme["line_top"]), 1.5))
        p.drawPolyline(QPolygonF(pts))
        p.restore()

    def _static_key(self):
        """静态层状态签名：变化时才重建静态层。"""
        return (self.width(), self.height(), self.log_scale, self.label_mode,
                self.transparent, self.show_border,
                round(self.freq_lo, 1), round(self.freq_hi, 1),
                round(self.db_lo, 1), round(self.db_hi, 1),
                len(self.engine.tracks),
                self.theme["bg"], self.theme["line_bottom"], self.theme["line_top"],
                self.theme["grid"], self.theme["text"], self.theme["border"])

    def _draw_fr_region(self, p, plot):
        """透明模式：填充耳机素质曲线（HD490 Pro 参考）以下，并画出分隔曲线。"""
        freqs = self.engine.freqs
        lo = max(1, int(np.searchsorted(freqs, self.freq_lo)) - 1)
        hi = min(len(freqs), int(np.searchsorted(freqs, self.freq_hi, side="right")) + 1)
        f = freqs[lo:hi]
        db = FR_REF_DB + headphone_fr(f)
        xs = self._freq_to_x(f, plot)   # 跟随 log/lin 缩放（原硬编码 log 导致 lin 模式曲线与 note 错位）
        frac = np.clip((db - self.db_lo) / (self.db_hi - self.db_lo), 0.0, 1.0)
        ys = plot.bottom() - frac * plot.height()
        pts = [QPointF(float(x), float(y)) for x, y in zip(xs, ys)]
        poly = QPolygonF(pts)
        poly.append(QPointF(plot.right(), plot.bottom()))
        poly.append(QPointF(plot.left(), plot.bottom()))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(self.theme["bg"]))
        p.drawPolygon(poly)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(self.theme["border"]), 1))
        p.drawPolyline(QPolygonF(pts))

    def _rebuild_static(self):
        """重建静态缓存（三层）：
        - _static_bg   背景+FR+网格（原生透明度）
        - _static_axis x/y 坐标刻度数字（恒不透明）
        - _static_soft 包络+图例+边框（原生透明度）
        紧凑模式（≤200px）：无背景色、无网格线，bg 层保持全透明。
        """
        self._static_bg = QPixmap(self.size())
        self._static_bg.fill(Qt.GlobalColor.transparent)
        p = QPainter(self._static_bg)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()
        if plot.width() > 0 and plot.height() > 0 and self.height() > HEIGHT_COMPACT:
            if self.transparent:
                self._draw_fr_region(p, plot)
            else:
                p.fillRect(self.rect(), QColor(self.theme["bg"]))
                # 非透明也画 FR 曲线参考线（填充被实色背景覆盖）——note 的 15px 锚点不消失
                self._draw_fr_region(p, plot)
            self._draw_grid(p, plot)
        p.end()

        self._static_axis = QPixmap(self.size())
        self._static_axis.fill(Qt.GlobalColor.transparent)
        p = QPainter(self._static_axis)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()
        if plot.width() > 0 and plot.height() > 0:
            self._draw_x_axis(p, plot)
            self._draw_y_axis(p, plot)
        p.end()

        self._static_soft = QPixmap(self.size())
        self._static_soft.fill(Qt.GlobalColor.transparent)
        p = QPainter(self._static_soft)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()
        if plot.width() > 0 and plot.height() > 0:
            for i, trk in enumerate(self.engine.tracks):
                self._draw_envelope(p, plot, trk,
                                    TRACK_COLORS[i % len(TRACK_COLORS)], dashed=False)
            self._draw_legend(p, plot)
        p.setPen(QPen(QColor(self.theme["border"]), BORDER))
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self.show_border:
            hb = BORDER / 2.0
            if self.height() <= HEIGHT_COMPACT:
                # 紧凑模式：无 FR 基准带 → 画左/右/下边框（无上边框）
                p.drawLine(QPointF(hb, hb), QPointF(hb, self.height() - hb))
                p.drawLine(QPointF(self.width() - hb, hb),
                           QPointF(self.width() - hb, self.height() - hb))
                p.drawLine(QPointF(hb, self.height() - hb),
                           QPointF(self.width() - hb, self.height() - hb))
            elif self.transparent:
                # 透明模式：只给基准线（FR_REF_DB）以下区域画边框（左、下、右）
                y_ref = self._db_to_y(FR_REF_DB, self._plot_rect())
                p.drawLine(QPointF(hb, self.height() - hb),
                           QPointF(self.width() - hb, self.height() - hb))
                p.drawLine(QPointF(hb, y_ref), QPointF(hb, self.height() - hb))
                p.drawLine(QPointF(self.width() - hb, y_ref),
                           QPointF(self.width() - hb, self.height() - hb))
            else:
                # 非透明模式：全窗左/右/下边框（无上边框）
                p.drawLine(QPointF(hb, hb), QPointF(hb, self.height() - hb))
                p.drawLine(QPointF(self.width() - hb, hb),
                           QPointF(self.width() - hb, self.height() - hb))
                p.drawLine(QPointF(hb, self.height() - hb),
                           QPointF(self.width() - hb, self.height() - hb))
        p.end()

    def _draw_envelope(self, p, plot, db_array, color, dashed):
        pts = self._bin_polygon(db_array, plot,
                                self.engine.freqs, self.engine.freqs_log10)
        if len(pts) < 2:
            return
        pen = QPen(QColor(color), 2.0)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(pen)
        p.drawPolyline(QPolygonF(pts))

    def _draw_legend(self, p, plot):
        """右上角图例：每条包络一个色块 + 编号。"""
        if not self.engine.tracks:
            return
        p.setFont(self.axis_font)
        x = plot.right() - 56
        y = plot.top() + 12
        for i in range(len(self.engine.tracks)):
            color = TRACK_COLORS[i % len(TRACK_COLORS)]
            p.setPen(QPen(QColor(color), 2))
            p.drawLine(QPointF(x, y), QPointF(x + 16, y))
            p.setPen(QPen(QColor(self.theme["text"])))
            p.drawText(QPointF(x + 20, y + 4), str(i + 1))
            y += 14

    def _draw_notes(self, p, plot):
        """乐理音高名称（垂直）。

        位置随 trans 模式：
        - trans 开：音名在频响曲线下方 15px（跟随曲线起伏）；
        - trans 关：音名固定在窗口上边缘下方 15px（顶部行）。
        与音量数字重叠时优先显示音名（音量数字在 _draw_note_markers 里跳过绘制），
        让音名盖住音量数字，避免文字数字糊在一起。
        """
        p.setFont(self.note_font)
        p.setPen(QPen(QColor("#c9a3b1")))
        for midi in range(MIDI_MAX + 1):
            f = note_frequency(midi)
            if f < self.freq_lo or f > self.freq_hi:
                continue
            x = self._freq_to_x(f, plot)
            if x < plot.left() or x > plot.right():
                continue
            if self.transparent:
                y_name = self._db_to_y(note_ref_db(f), plot) + NOTE_FR_OFFSET_PX
            else:
                y_name = 15.0   # 窗口上边缘下方 15px
            name = note_name(midi)
            p.save()
            p.translate(x, y_name)
            p.rotate(-90)
            p.drawText(QRectF(-16, -6, 32, 12),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                       name)
            p.restore()

    def _draw_note_markers(self, p, plot):
        """在频谱上标注 E0~C7 每个音高的当前音量：'+70dBFS' 显示值（dBFS+70，下限 -40 钳制）。

        颜色随显示值符号：负 → 偏红，正 → 偏绿（0 及以上归绿）。
        """
        p.setFont(self.note_font)
        freqs = self.engine.freqs
        cur = self.engine.current
        for midi in range(16, MIDI_MAX + 1):  # E0 (MIDI 16) 到显示上限（~D#10）
            f = note_frequency(midi)
            if f < self.freq_lo or f > self.freq_hi:
                continue
            x = self._freq_to_x(f, plot)
            if x < plot.left() or x > plot.right():
                continue
            idx = max(0, min(len(freqs) - 1, int(np.searchsorted(freqs, f))))
            db = cur[idx]
            y = self._db_to_y(db, plot)
            y = max(plot.top(), min(plot.bottom(), y))
            disp = db_display(db)
            # 紧凑模式 + Note 模式：底部 x 轴音名在 plot.bottom 附近，重叠时让音名盖住音量数字（跳过绘制）
            if self.height() <= HEIGHT_COMPACT and self.label_mode == LABEL_NOTES \
                    and y > plot.bottom() - NOTE_XAXIS_OVERLAP_PX:
                continue
            # 详情模式（>200px）+ Note 模式：音名在 FR 下方 15px（trans 开）或顶行（trans 关），
            # 与音名重叠时让音名盖住音量数字（跳过绘制音量）
            if self.height() > HEIGHT_COMPACT and self.label_mode == LABEL_NOTES:
                if self.transparent:
                    y_name = self._db_to_y(note_ref_db(f), plot) + NOTE_FR_OFFSET_PX
                else:
                    y_name = 15.0
                if abs(y - y_name) < 14:
                    continue
            label = f"{disp:.0f}"
            color = "#e57373" if disp < 0 else "#81c784"
            p.setPen(QPen(QColor(color)))
            p.save()
            p.translate(x, y)
            p.rotate(-90)
            p.drawText(QRectF(-15, -7, 30, 14),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                       label)
            p.restore()

    def _draw_rec_indicator(self, p):
        """录制中：顶栏红点 + REC。"""
        cy = BORDER + TOP_BAR / 2.0
        cx = self.width() / 2.0 - 24
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#ff4444"))
        p.drawEllipse(QPointF(cx, cy), 4, 4)
        p.setPen(QColor("#ff4444"))
        p.setFont(self.axis_font)
        p.drawText(QPointF(cx + 8, cy + 4), "REC")

    def _draw_fps(self, p):
        """顶栏右侧：曲线更新率（Hz）+ 回调异常计数（>0 时红字提示）。"""
        if self.update_rate <= 0:
            return
        p.setFont(self.axis_font)
        p.setPen(QColor("#66707a"))
        p.drawText(QRectF(self._close_x - 90, self._dock_y(), 80, TOP_BAR),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   f"{self.update_rate:.0f} Hz")
        errs = getattr(self.engine, "cb_errors", 0)
        if errs > 0:
            p.setPen(QColor("#ff4444"))
            p.drawText(QRectF(self._close_x - 160, self._dock_y(), 65, TOP_BAR),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       f"E{errs}")

    def _draw_x_axis(self, p, plot):
        if self.label_mode == LABEL_MIN:
            return   # Min 模式：隐藏全部刻度数字
        # 紧凑模式 + Note 模式：x 轴频率数字替换为音名（去八度、顺时针旋转 90°）
        if self.height() <= HEIGHT_COMPACT and self.label_mode == LABEL_NOTES:
            self._draw_x_axis_notes(p, plot)
            return
        p.setFont(self.axis_font)
        p.setPen(QPen(QColor(self.theme["text"])))
        fm = p.fontMetrics()
        if self.log_scale:
            ticks = sorted(log_ticks(self.freq_lo, self.freq_hi)
                           + fine_freq_ticks(self.freq_lo, self.freq_hi))
        else:
            ticks = lin_ticks(self.freq_lo, self.freq_hi)
        last_right = plot.left() - 1
        for v in ticks:
            x = self._freq_to_x(v, plot)
            label = format_freq(v)
            w = fm.horizontalAdvance(label)
            x = min(max(x, plot.left() + w / 2.0), plot.right() - w / 2.0)
            # 有空间才画标签，避免重叠（网格线仍全部画）
            if x - w / 2.0 < last_right + 4:
                continue
            p.drawText(QPointF(x - w / 2.0, plot.bottom() - 3), label)
            last_right = x + w / 2.0

    def _draw_x_axis_notes(self, p, plot):
        """紧凑模式（≤200px）+ Note 模式：x 轴每个半音都标注音名（去八度）。

        正常水平书写（从左到右）；颜色沿用原刻度数字（theme.text）；G#6 → G#。
        """
        p.setFont(self.axis_font)
        p.setPen(QPen(QColor(self.theme["text"])))
        fm = p.fontMetrics()
        last_right = plot.left() - 1
        for midi in range(16, MIDI_MAX + 1):  # E0 (MIDI 16) 到显示上限（~D#10）
            f = note_frequency(midi)
            if f < self.freq_lo or f > self.freq_hi:
                continue
            x = self._freq_to_x(f, plot)
            if x < plot.left() or x > plot.right():
                continue
            # C 音标注八度（C4），其余音保持无八度（G#）
            label = note_name(midi) if midi % 12 == 0 else note_name_short(midi)
            w = fm.horizontalAdvance(label)
            x = min(max(x, plot.left() + w / 2.0), plot.right() - w / 2.0)
            # 有空间才画标签，避免重叠
            if x - w / 2.0 < last_right + 4:
                continue
            p.drawText(QPointF(x - w / 2.0, plot.bottom() - 3), label)
            last_right = x + w / 2.0

    def _draw_y_axis(self, p, plot):
        if self.label_mode == LABEL_MIN:
            return   # Min 模式：隐藏全部刻度数字
        p.setFont(self.axis_font)
        p.setPen(QPen(QColor(self.theme["text"])))
        for v in db_ticks(self.db_lo, self.db_hi):
            disp = int(v) + int(DB_DISPLAY_OFFSET)   # '+70dBFS' 单位
            if (abs(disp) // 10) % 2 == 1:
                continue   # 十位数为奇数的数字去掉（挤；网格线仍全画）
            y = self._db_to_y(v, plot)
            p.save()
            p.translate(plot.left() + 6, y)
            p.rotate(-90)
            p.drawText(QRectF(-20, -10, 40, 20),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                       f"{disp}")
            p.restore()


def _pick_font(point_size):
    """优先几何感无衬线字体，逐级回退。"""
    families = set(QFontDatabase.families())
    for name in ("Bahnschrift", "Segoe UI", "Consolas"):
        if name in families:
            return QFont(name, point_size)
    return QFont("Segoe UI", point_size)


# ---- 配置加载 ----

def load_config(widget, path):
    """读取 CAVA 风格 config，更新主题/视图/平滑参数。"""
    cp = configparser.ConfigParser()
    if not cp.read(path, encoding="utf-8"):
        raise SystemExit(f"无法读取配置文件: {path}")

    if cp.has_section("general"):
        if cp.has_option("general", "framerate"):
            widget.fps = max(1, cp.getint("general", "framerate"))
        if cp.has_option("general", "lower_cutoff_freq"):
            widget.freq_lo = max(FREQ_ABS_MIN, cp.getfloat("general", "lower_cutoff_freq"))
        if cp.has_option("general", "higher_cutoff_freq"):
            widget.freq_hi = min(FREQ_ABS_MAX, cp.getfloat("general", "higher_cutoff_freq"))

    if cp.has_section("theme"):
        if cp.has_option("theme", "theme"):
            widget.set_theme(cp.get("theme", "theme"))
        mapping = {"background": "bg", "line_bottom": "line_bottom", "line_top": "line_top",
                   "grid": "grid", "text": "text", "border": "border"}
        for cfg_key, theme_key in mapping.items():
            if cp.has_option("theme", cfg_key):
                widget.theme[theme_key] = cp.get("theme", cfg_key).strip()

    if cp.has_section("color"):
        if cp.has_option("color", "background"):
            widget.theme["bg"] = cp.get("color", "background").strip()
        if cp.has_option("color", "gradient_color_1"):
            widget.theme["line_bottom"] = cp.get("color", "gradient_color_1").strip()
        if cp.has_option("color", "gradient_color_2"):
            widget.theme["line_top"] = cp.get("color", "gradient_color_2").strip()
        if cp.has_option("color", "foreground"):
            widget.theme["line_top"] = cp.get("color", "foreground").strip()

    if cp.has_section("smoothing"):
        if cp.has_option("smoothing", "attack"):
            widget.engine.attack = min(1.0, max(0.0, cp.getfloat("smoothing", "attack")))
        if cp.has_option("smoothing", "release"):
            widget.engine.release = min(1.0, max(0.0, cp.getfloat("smoothing", "release")))

    if cp.has_section("window"):
        if cp.has_option("window", "opacity"):
            widget.opacity = min(1.0, max(0.05, cp.getfloat("window", "opacity")))

    if hasattr(widget, "_apply_slider_style"):
        widget._apply_slider_style()   # 主题覆盖后刷新滑块配色

    widget.db_lo, widget.db_hi = clamp_db(widget.db_lo, widget.db_hi, widget.db_span)


def _resource_path(name):
    """打包后从 PyInstaller 临时目录读资源，否则从脚本目录读。"""
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, name)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


# ---- 入口 ----

def main():
    # Windows 控制台默认 cp1252，打印非 ASCII 设备名会 UnicodeEncodeError → 强制 UTF-8
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="原生 FFT 实时频谱（PyQt6 平滑折线图）")
    ap.add_argument("--list", action="store_true", help="列出所有输入设备")
    ap.add_argument("--device", type=int, default=None, help="指定输入设备索引")
    ap.add_argument("--fps", type=int, default=None, help=f"刷新率（默认 {FPS}）")
    ap.add_argument("--attack", type=float, default=None, help=f"上升权重 0~1（默认 {ATTACK}）")
    ap.add_argument("--release", type=float, default=None, help=f"下降权重 0~1（默认 {RELEASE}）")
    ap.add_argument("--config", default=None, help="CAVA 风格配置文件路径（可选）")
    args = ap.parse_args()

    if args.list:
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                print(f"{i:3d}: {d['name']}")
        return

    # 单实例：开机自启 + 手动打开不叠窗（named mutex，跨 python/exe 进程生效）
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW(None, False, "spectrum_single_instance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        print("spectrum 已在运行", file=sys.stderr)
        return

    if args.device is not None:
        info = sd.query_devices(args.device)
        if info["max_input_channels"] <= 0:
            raise SystemExit(f"设备 {args.device} 不是输入设备")

    engine = SpectrumEngine()
    app = QApplication(sys.argv)
    w = SpectrumWindow(engine, config_path=args.config, device=args.device)

    # 任务栏图标：frameless 窗口默认不继承 exe 图标，需显式设置
    icon_path = _resource_path("icon.ico")
    if os.path.exists(icon_path):
        icon = QIcon(icon_path)
        app.setWindowIcon(icon)
        w.setWindowIcon(icon)

    if args.fps is not None:
        w.fps = max(1, args.fps)
    if args.attack is not None:
        w.engine.attack = min(1.0, max(0.0, args.attack))
    if args.release is not None:
        w.engine.release = min(1.0, max(0.0, args.release))
    if args.config:
        load_config(w, args.config)

    w._open_stream()
    if w.stream is None:
        print("警告：没有可用的音频输入设备", file=sys.stderr)

    # PreciseTimer：否则 Windows 默认 ~15.6ms 分辨率会把 180 FPS 压到 ~64 FPS
    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.timeout.connect(w.advance)
    timer.start(max(1, int(1000 / max(1, w.fps))))

    app.aboutToQuit.connect(w.shutdown)
    w.show()
    w._apply_topmost()   # 默认置顶
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
