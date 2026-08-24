"""纯逻辑层单测：刻度、格式化、dB 视口约束、缩放、频响基准、主题。"""
import pytest

from spectrum import (FR_REF_DB, MIDI_MAX, NOTE_FR_OFFSET_PX, _mix_white,
                      THEMES, THEME_ORDER, adhere_step, clamp_db, db_display,
                      db_ticks, ease_out_cubic, fine_freq_ticks, floor_baseline,
                      format_freq, freq_to_midi, headphone_fr, lin_ticks, log_ticks,
                      mode_anchor, mode_forced_preview, note_frequency, note_name,
                      note_name_short, note_ref_db, should_follow_taskbar,
                      zoom_db_view, zorder_insert)


def test_log_ticks_all_digits():
    ticks = log_ticks(20, 20000)
    assert ticks == sorted(ticks)
    assert all(20 <= t <= 20000 for t in ticks)
    assert 20 in ticks and 30 in ticks and 90 in ticks    # 1~9 全
    assert 100 in ticks and 900 in ticks
    assert 1000 in ticks and 9000 in ticks and 20000 in ticks


def test_fine_freq_ticks():
    ticks = fine_freq_ticks(20, 20000)
    assert 11000 in ticks and 15000 in ticks and 19000 in ticks
    assert fine_freq_ticks(20, 9000) == []


def test_log_ticks_empty_on_bad_range():
    assert log_ticks(0, 100) == []
    assert log_ticks(20000, 20) == []


def test_db_ticks():
    assert db_ticks(-120, 0) == [-120, -110, -100, -90, -80, -70, -60,
                                 -50, -40, -30, -20, -10, 0]


def test_lin_ticks():
    ticks = lin_ticks(20, 20000)
    assert ticks == sorted(ticks)
    assert all(20 <= t <= 20000 for t in ticks)
    assert 2000 in ticks and 20000 in ticks
    assert 10 <= len(ticks) <= 13       # 自适应步长 → 约 10 个刻度


def test_format_freq():
    assert format_freq(20) == "20"
    assert format_freq(500) == "500"
    assert format_freq(1000) == "1k"
    assert format_freq(2000) == "2k"
    assert format_freq(20000) == "20k"


def test_note_frequency_and_name():
    assert note_name(69) == "A4"
    assert note_frequency(69) == pytest.approx(440.0)
    assert note_name(60) == "C4"
    assert note_frequency(60) == pytest.approx(261.63, rel=1e-2)
    assert note_name(61) == "C#4"


def test_note_name_short():
    # 紧凑模式 x 轴音名：去八度（G#6 → G#）
    assert note_name_short(69) == "A"
    assert note_name_short(60) == "C"
    assert note_name_short(61) == "C#"
    assert note_name_short(68) == "G#"
    assert note_name_short(127) == "G"


def test_freq_to_midi():
    assert freq_to_midi(440.0) == 69
    assert freq_to_midi(261.63) == 60
    assert freq_to_midi(1000.0) == 83     # B5 ≈ 987.8Hz
    assert 0 <= freq_to_midi(20.0) <= 16  # 低频近 E0


def test_db_display_plus70():
    # '+70dBFS' 单位：dBFS + 70；下限 -40 钳制（= -110dBFS）
    assert db_display(-80.0) == pytest.approx(-10.0)
    assert db_display(-70.0) == pytest.approx(0.0)      # FR 基准线显示为 0
    assert db_display(-110.0) == pytest.approx(-40.0)   # 恰在钳制点
    assert db_display(-200.0) == pytest.approx(-40.0)   # 低于钳制点 → 停住不再波动
    assert db_display(0.0) == pytest.approx(70.0)


def test_floor_baseline():
    import numpy as np
    bottom = 100.0
    # 显示区内的点保持原样；底部之外的点拉回 1px 底线
    assert floor_baseline(np.array([10.0, 50.0, 90.0]), bottom).tolist() == [10.0, 50.0, 90.0]
    # 恰在底线上/下的点 → 钳到 99（1px 内）
    assert floor_baseline(np.array([100.0, 99.5, 101.0]), bottom).tolist() == [99.0, 99.0, 99.0]
    # 静音（全部在底部外）→ 平线在 1px 内，不整条压平
    assert floor_baseline(np.array([100.0, 120.0, 200.0]), bottom).tolist() == [99.0, 99.0, 99.0]


def test_note_extended_to_20k():
    # 标准 MIDI 顶到 G9 (12.5k)；MIDI_MAX 扩展公式让标注铺满到 ~20k（D#10 ≈ 19.9k）
    assert note_name(127) == "G9"
    assert note_frequency(127) == pytest.approx(12543.85, rel=1e-3)
    assert note_name(MIDI_MAX) == "D#10"
    assert note_frequency(MIDI_MAX) == pytest.approx(19912.13, rel=1e-3)
    assert note_frequency(MIDI_MAX) <= 20000
    assert note_frequency(MIDI_MAX + 1) > 20000


def test_mix_white():
    assert _mix_white("#000000", 0.0) == "#000000"
    assert _mix_white("#000000", 1.0) == "#ffffff"
    assert _mix_white("#ffffff", 0.5) == "#ffffff"


def test_headphone_fr():
    assert headphone_fr(20.0) == pytest.approx(-4.3, abs=0.1)
    assert headphone_fr(1000.0) == pytest.approx(0.0, abs=0.15)
    assert headphone_fr(3000.0) > 3.0      # 3kHz 峰值


def test_clamp_db_default_unchanged():
    # 默认 span=110：-110..0 合法 → 原样
    assert clamp_db(-110, 0) == (-110, 0)


def test_clamp_db_scroll_up_ceiling():
    # 滚到最上 → -110..0
    assert clamp_db(-140, 50) == (-110, 0)


def test_clamp_db_scroll_down_floor():
    # 滚到最下 → -140..-30
    assert clamp_db(-200, -70) == (-140, -30)


def test_clamp_db_fixed_span():
    lo, hi = clamp_db(-123, -5)
    assert hi - lo == pytest.approx(110)
    assert lo >= -140 and hi <= 0


def test_clamp_db_span_arg():
    # span=120：db_hi 从 -30 推到 -20（下界 -140+120=-20），lo=-140
    assert clamp_db(-100, -30, span=120) == (-140, -20)
    # span=140：全范围 -140..0
    assert clamp_db(-100, -30, span=140) == (-140, 0)


def test_zoom_db_view_zoom_out():
    # 滚轮下（step=-1）：span 110→120，中线 -55 锚定 → 钳到 -120..0
    assert zoom_db_view(-110, 0, 110.0, -1.0) == (-120, 0, 120)


def test_zoom_db_view_zoom_in_clamped():
    # 滚轮上（step=+1）：span 已到下限 110 → 原样返回
    assert zoom_db_view(-110, 0, 110.0, 1.0) == (-110, 0, 110)


def test_zoom_db_view_max_clamp():
    # span 135 +1 步 → 钳到上限 140，视口推到全范围 -140..0
    assert zoom_db_view(-100, -30, 135.0, -1.0) == (-140, 0, 140)


def test_zoom_db_view_cursor_anchor():
    # 锚点 -100：span 120 → 视口钳到 -140..-20，锚点位于下 1/3
    assert zoom_db_view(-110, 0, 110.0, -1.0, anchor_db=-100.0) == (-140, -20, 120)


def test_fr_ref_db():
    # 透明边缘标准点：1kHz 归零 → -70 dBFS（2026-08-18 由 -60 下调）
    assert FR_REF_DB == -70.0
    assert NOTE_FR_OFFSET_PX == 15.0


def test_note_ref_db_follows_curve():
    assert note_ref_db(1000.0) == pytest.approx(-70.0, abs=0.15)
    assert note_ref_db(3000.0) > -67.0        # 3kHz 峰抬升曲线 → 文字上移
    assert note_ref_db(13625.8) < -80.0       # 9~14k 深谷 → 文字下沉


def test_themes_complete():
    assert len(THEMES) == 1
    assert "sunset" in THEMES
    for key in ("bg", "line_bottom", "line_top", "grid", "text", "border"):
        assert key in THEMES["sunset"], f"sunset 缺字段 {key}"


def test_mode_forced_preview():
    # top 关 → 强制预览模式（高度300/tint80），与 trans 状态无关（2026-08-20）
    assert mode_forced_preview(False) is True
    assert mode_forced_preview(True) is False


def test_mode_anchor():
    # 非置顶 → 贴任务栏上方；trans 开+top 开 → 盖任务栏；trans 关+top 开 → 贴任务栏跟随
    assert mode_anchor(True, True) == "over_taskbar"
    assert mode_anchor(False, True) == "adhere"
    assert mode_anchor(True, False) == "above_taskbar"
    assert mode_anchor(False, False) == "above_taskbar"


def test_should_follow_taskbar():
    # 底边在目标阈值内 → 跟随；用户拖远 → 不打扰
    assert should_follow_taskbar(1000, 1031) is True       # 差 31 < 250
    assert should_follow_taskbar(1079, 1031) is True       # 差 48 < 250
    assert should_follow_taskbar(600, 1079) is False       # 差 479 > 250
    assert should_follow_taskbar(1080, 1079) is True       # 差 1 ≤ 阈值


def test_ease_out_cubic():
    # 贴任务栏固定动画缓动（起步快收尾缓）：端点/前载/单调/越界钳制
    assert ease_out_cubic(0.0) == 0.0
    assert ease_out_cubic(1.0) == 1.0
    assert ease_out_cubic(0.5) == pytest.approx(0.875)   # 过半即完成 87.5% → 起步快、收尾缓
    assert ease_out_cubic(0.5) > 0.5                     # 前段加速快（与 easeIn 相反）
    assert ease_out_cubic(-1.0) == 0.0                   # 越界钳制
    assert ease_out_cubic(2.0) == 1.0
    ys = [ease_out_cubic(t) for t in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)]
    assert ys == sorted(ys)                              # 单调


def test_adhere_step():
    # 贴任务栏固定动画：t=0/1 端点、0.5 前载，全程单调不抖回
    assert adhere_step(100, 200, 0.0) == pytest.approx(100.0)
    assert adhere_step(100, 200, 1.0) == pytest.approx(200.0)
    assert adhere_step(100, 200, 0.5) == pytest.approx(187.5)   # easeOut：起步快
    ys = [adhere_step(100, 200, t) for t in (0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0)]
    assert ys == sorted(ys)                                  # 向上滑不抖回


def test_zorder_insert():
    # top 开 → HWND_TOPMOST(-1)；top 关 → HWND_BOTTOM(1) 真正最底
    assert zorder_insert(True) == -1
    assert zorder_insert(False) == 1
