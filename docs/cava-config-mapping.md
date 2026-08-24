# CAVA 配置 → spectrum.py 映射

目标：把 CAVA 的 `~/.config/cava/config` 常用参数对应到 `../spectrum.py`，方便后续实现 `--config` 读取。

## 映射表

| CAVA 配置项 | 含义 | spectrum.py 当前对应 |
|-------------|------|----------------------|
| `[general] bars` | 柱数 | `N_BARS = 48` |
| `[general] framerate` | 刷新率 | `FPS = 180` |
| `[general] lower_cutoff_freq` | 最低频率 | `FREQ_MIN = 20` |
| `[general] higher_cutoff_freq` | 最高频率 | `FREQ_MAX = 20000` |
| `[general] sensitivity` | 灵敏度 | 暂无（当前固定 `DB_LO/HI`） |
| `[general] autosens` | 自动灵敏度 | 暂无 |
| `[output] channels` | 单声道/立体声 | 当前取均值后单声道显示 |
| `[color] background` | 背景色 | `BG = "#0a0e14"` |
| `[color] foreground` | 前景/柱顶颜色 | `BAR_TOP = "#00e5c0"` |
| `[color] gradient` | 渐变开关 | 当前恒为渐变 |
| `[color] gradient_color_1` | 渐变底部 | `BAR_BOTTOM = "#0b3b5a"` |
| `[color] gradient_color_2` | 渐变顶部 | `BAR_TOP` |
| `[smoothing] noise_reduction` | 噪声抑制 | 暂无 |
| `[smoothing] decay` | 柱回落速度 | `SMOOTH = 0.25`（相反语义） |

## 当前进度

- [x] `spectrum.py --config <path>` 已支持读取 `[general]`、`[color]`、`[smoothing]` 中的常用项。
- [x] 配置加载后重建 FFT 分桶映射（`rebuild_fft_mapping()`）。
- [ ] 自动灵敏度 `autosens` / `sensitivity` 尚未实现。
- [ ] 噪声抑制 `noise_reduction` 尚未实现。
- [ ] 立体声左右声道分离/镜像尚未实现。
