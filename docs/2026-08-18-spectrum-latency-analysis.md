# 8cava 实时频谱延迟问题 — 技术分析文档

日期：2026-08-18
状态：分析 / 待定路线
范围：`spectrum.py` 采集 → FFT → 渲染全链路延迟优化

---

## 1. 项目全景

### 1.1 现状

8cava 是一个 PyQt6 自绘实时频谱（黑底渐变折线），运行在 Windows 底部透明条上。核心用途是 **HD490 Pro EQ 预设可视化验证**（max-hold 包络录制对比）。

管线：

```
采集（sounddevice，立体声混音 loopback）
  → FFT（numpy rfft，环形缓冲滑动窗）
  → 时间 EMA（noise_reduction=0.5）
  → 渲染 EMA（smooth=0.25，QTimer 180 FPS）
  → 自绘（静态层缓存 + 动态折线，QPainter）
```

当前关键参数（2026-08-18 变更后）：

| 参数 | 值 | 含义 |
|------|-----|------|
| `SAMPLERATE` | 48000 Hz | — |
| `FFT_SIZE` | **65536** | FFT 窗长 → 时间窗 **1.365 s** |
| `HOP` | 1024 样本 | 输入块 → 更新率 ~47 Hz（21.3 ms/块） |
| `smooth` | 0.25 | 渲染 EMA 系数 |
| `noise_reduction` | 0.5 | mag EMA 系数 |
| FPS | 60~180 | 画面刷新率 |

### 1.2 症状

- 帧率很高（180 FPS），画面"转起来"不卡。
- 但**跟踪拖沓**：鼓点/人声/瞬态响应迟钝，新声音要 ~1.4 s 才在窗口内"铺满"，停止后余音缓慢消退。
- 用户描述："帧率挺高的，但是采样频率不够高，整体非常拖沓。"

### 1.3 矛盾点

用户最近的两个诉求存在物理冲突：

1. **低频精细**（2026-08-18）：20 Hz 处至少 8 px 一个采样点 → 要求 bin ≤ 0.44 Hz → FFT ≥ 65536。
2. **实时跟手**（2026-08-18 二次反馈）：瞬态响应快、不拖沓 → 要求时间窗 ≤ 50~100 ms → FFT ≤ 2048~4096。

这两个目标由**时频不确定性原理**绑定，单一均匀 FFT 不可能同时满足。

---

## 2. 延迟成因分析

### 2.1 延迟构成实测推算

| 延迟源 | 当前值 | 说明 |
|--------|--------|------|
| 输入块（HOP=1024） | ~21 ms | sounddevice 每 21.3 ms 收一批（blocksize=HOP） |
| **FFT 窗组延迟（65536）** | **~683 ms** | 窗长 1.365 s，组延迟 ≈ 半窗。**绝对主导** |
| mag EMA（nr=0.5） | ~42 ms | 每 2 个 FFT 更新收敛 |
| 渲染 EMA（smooth=0.25） | ~22 ms | @180 FPS 时 4 帧收敛 |
| **合计有效滞后** | **≈ 0.8 s** | — |

> 注：此前 2026-08-18 将 FFT 从 16384（0.34 s 窗）提到 65536（1.37 s 窗），是为满足"8px 低频"要求，代价是时间响应恶化 4 倍。**这次拖沓是本轮改动的直接副作用。**

### 2.2 根因：时频不确定性

均匀 FFT 下，频率分辨率（bin 宽）与时间分辨率（窗长）乘积近似为常数：

```
bin 宽 = SAMPLERATE / FFT_SIZE
时间窗 = FFT_SIZE / SAMPLERATE
bin 宽 × 时间窗 = 1
```

- 要 20 Hz 处 bin ≤ 0.44 Hz（8 px）→ 时间窗 ≥ 1.1 s → 天生拖沓。
- 要时间窗 ≤ 50 ms → bin 宽 ≥ 21.5 Hz → 20~100 Hz 对数轴 bin 间隔 ≥ 数十 px，低频必然稀疏。

**结论：物理上不存在"均匀 FFT 下既 8px 低频又不拖沓"的方案。** 出路只能是：
1. 放弃 8px（用短窗 + 渲染插值平滑观感）；
2. 或分频段多分辨率（低频长窗保精细、高频短窗保跟手）；
3. 或显示/录制分流（实时显示用快窗，max-hold 录制用长窗）。

---

## 3. 对标分析：mineradio 如何做到"无延迟"

Mineradio（`D:\Program Files\mineradio\body2.1.0\`，Electron 网易云系收音机）是本机已验证"无延迟"的实时频谱参考。源码实证：

### 3.1 管线（源码证据）

- **FFT_SIZE = 2048**（`public/js/modules/00-state/00-core-stores.js`）：
  - 时间窗 = 2048/44100 ≈ **46 ms**，组延迟 ≈ **23 ms** —— 近零感知延迟。
  - 频率 bin = 44100/2048 ≈ 21.5 Hz（1024 bins 覆盖 0~22 kHz）。
- **双 Analyser 分流**（`05-playback/08-audio-graph-controls.js`）：
  - `analyser.fftSize = 2048; smoothingTimeConstant = 0.58`（显示用，轻平滑）
  - `beatAnalyser.fftSize = 2048; smoothingTimeConstant = 0.10`（节拍用，**近零平滑、攻击极快**）
- **rAF 渲染循环拉数据**（`11-main-loop.js`）：
  - 每动画帧 `analyser.getByteFrequencyData(frequencyData)` + `getByteTimeDomainData(timeDomainData)`。
  - Web Audio 的 Analyser 在**音频线程原生计算**，读取非阻塞、与渲染帧同步。
- **频段聚合而非逐 bin**（`03-beat/06-sonic-audio-monitor.js`）：
  - 频谱从不做精细低频线，而是聚合成 8 个感知频段：`subBass(32–58Hz) / bass / lowMid / mid / highMid / presence / brilliance / air(9–16kHz)`。
  - 节拍检测用更窄窗（Deep 36–82Hz 等），也全是宽频带能量，不依赖低频单 bin 精细度。

### 3.2 结论

**mineradio 的"无延迟"配方 = 小 FFT 窗（46 ms）+ 频段能量聚合 + 音频线程原生分析 + rAF 同步拉取。**

它用 21.5 Hz 的粗 bin 换来 23 ms 的即时响应。它的使用场景（可视化 bar + 节拍/鼓点检测）**根本不需要低频精细 bin**，所以不存在我们面对的取舍。

### 3.3 与 8cava 的差异对照

| 维度 | mineradio | 8cava（当前） |
|------|-----------|---------------|
| FFT_SIZE | 2048 | **65536**（32 倍） |
| 时间窗 | 46 ms | 1.365 s |
| 组延迟 | ~23 ms | ~683 ms |
| 低频呈现 | 8 个宽频段能量 | 逐 bin 折线（要 8px） |
| 平滑 | 0.10~0.58 | 0.5 + 0.25（双重 EMA） |
| 分析线程 | Web Audio 原生 | numpy 在回调线程 |

---

## 4. 行业通用方案（网络调研）

同类工具（WASAPI loopback 频谱仪）的共识做法，见附录链接：

### 4.1 采集层
- **WASAPI loopback** 直接读系统混音输出，免虚拟声卡（SpectrumNet / SpectrumCpp / BeSpec / CSCore 例程）。
- 低延迟需要**小 buffer period**（~10 ms）；要 bit-perfect 需 **WASAPI exclusive 或 ASIO**（绕过系统 SRC）。
- PortAudio 不直接支持 loopback，需自写 WASAPI 扩展或用 CSCore/cpal 等封装。

### 4.2 分析层
- **FFT 尺寸主流 2048–4096**（BeSpec 固定 2048；CSCore 例程 4096"典型值"）。
- **环形缓冲 + 工作线程解耦**：WASAPI 采集线程只填 ring buffer（CriticalSection 轻锁），渲染线程按需取最新 2048 样本 FFT；丢帧不影响音频（Irrlicht 例程，60 FPS < 1% CPU）。
- 参考端到端指标：aiXander 的 Python 实时分析，**输入→输出 8–15 ms**，每块 FFT（hop=512/48k ≈ 94 Hz 更新）。

### 4.3 显示层
- **对数/感知频段**（octave 均匀分布 `f×2^(1/n)`），而非线性逐 bin —— 把"看得细"与"需要细 bin"解耦。
- **Attack/Release 动态 + peak hold**：后处理整形，不增加输入延迟，让显示"响应快又稳"。
- **分配零开销回调**：音频线程零 DSP，全部 FFT/平滑放工作线程（numpy 向量化）。

### 4.4 关键启示

> **"无延迟"的第一要素不是语言、不是渲染引擎，而是 FFT 窗长（2048–4096 → 几十 ms）。**
> 8cava 的 65536 窗是行业标准的 16–32 倍，这解释了全部拖沓。

---

## 5. 技术路线（候选方案）

### 方案 A — 快改（常量级，1 分钟见效）【✅ 2026-08-18 已实施】

| 改动 | 值 | 说明 |
|------|-----|------|
| `FFT_SIZE` | 65536 → **8192** | 窗 0.171 s，组延迟 85 ms（主修复） |
| `HOP` | 1024 → **512** | 输入块 10.7 ms，更新率 ~94 Hz |
| `smooth` | 0.25 → **0.40** | 渲染 EMA 越大跟得越快（`current += (target-current)*smooth`） |
| `noise_reduction` | 0.5 → **0.3** | 时间 EMA 越小攻击越快 |

> 修正：初稿把 `smooth` 写成 0.25→0.15 是**方向错误**——`smooth` 是 `current += (target-current)*smooth`，值越小收敛越慢、越拖。降延迟应调大。

- 效果：总滞后 ~0.8 s → **~0.12 s**（快 ~6.7 倍）；更新率翻倍（47→94 Hz），"采样频率"跟手。
- 代价：**低频 bin 变稀**——8192 时 20~50 Hz 约 56 px/bin，50~200 Hz 约 20 px/bin。折线直连 bin 中心渲染（**不做插值，保持测量诚实**），放弃"8px 原始采样"。
- 工作量：改 4 个常量 + 同步 config 示例；风险低。**已实施**（`spectrum.py` + `cava.config.example` + README）。

### 方案 B — 分层架构（最终形态，GPT 对齐）【✅ 2026-08-18 已实施】

演进：2 段（500Hz 拼接）→ 8 段恒定-Q（用户否决"低频慢动态"，且 GPT 指出：只要实时线仍由长窗驱动，kick 必被 1.37s 窗抹平）→ **最终分层**：实时线与精细分析彻底分离，互不污染。

```
输入音频 ──┬→ 短窗 4096 FFT ─────────→ 实时主线（全频段跟手，50Hz kick 立即动）
          ├→ 长窗 65536 FFT（20-500Hz，节流）→ 暗色精细参考线（低频真实结构，慢）
          └→ 合并网格（长窗低频+短窗高频）→ 录制 max-hold 包络（EQ 对比）
```

| 层 | 数据源 | 网格 | 动态 | 用途 |
|----|--------|------|------|------|
| 实时线 | 短窗 4096 | 2049 bins | 43ms | 主线，鼓点/人声/镲片，**含 50Hz kick** |
| 精细参考线 | 长窗 65536（20-500Hz） | 683 bins | 1.37s | 暗色细线，显示低频真实频率结构 |
| 录制包络 | 长窗低频 + 短窗高频 | 2689 bins | 慢/无所谓 | max-hold，EQ 预设对比 |

- **非对称 A/R**：`attack=0.80`/`release=0.25`（替代单 EMA）——上升快冲、下降慢落，保留 kick 独立峰形。
- **关键洞察**（GPT）：50Hz 精细频谱（0.44Hz 分辨）需数秒观测；50Hz kick 每脚响度需宽带包络。**二者不能由同一条长窗线承担**。实时线放弃低频真分辨（显示级平滑），精细结构交给独立参考层/录制层。
- **资源实测**：长窗每 8 回调（~23.5Hz），rfft(65536) ~1.1ms，预算内；短窗每回调。合计 ~3% 核心。
- 工作量：中（引擎双路径 + 渲染泛化投影）；风险：中低。

### 方案 C — 折中

`FFT_SIZE` 65536 → **16384**（窗 0.341 s，组延迟 170 ms）。比现状快 4 倍；低频 bin 34 px + 插值。跟手度、分辨率都"够用但不极致"。一行改。

### 方案 D — 显示/录制分流（面向用途）

- **实时显示**：短窗（如 4096，85 ms）→ 跟手。
- **max-hold 包络录制**（EQ 验证主用途）：长窗（如 65536）→ 低频精细，EQ 对比准确。
- 包络录制本来就不需要实时性（累计最大值），与显示解耦最合理。
- 代价：引擎需维护两套 FFT 输出；工作量中偏大，可与方案 B 结合。

### 对比与推荐

| 方案 | 跟手度 | 低频 8px | 工作量 | 风险 |
|------|--------|----------|--------|------|
| A（8192+插值） | ★★★★★ | ✗（插值补） | ★ | 低 |
| B（两级 500Hz 拼接） | ★★★★☆ 高频 / 低频仍慢 | ✓ | ★★★ | 中 |
| C（16384） | ★★★☆ | ✗（插值补） | ★ | 低 |
| D（分流）+B | ★★★★★ 显示 | ✓ 录制 | ★★★★ | 中高 |

**推荐路径**：先 A（1 分钟验证"跟手"到底够不够）；若低频观感无法接受 → 上 **B**（两级 FFT）；EQ 验证精度需求高 → 再叠 **D**（录制走长窗）。C++ 改写**不解决此问题**（见 §6），不必为延迟改写。

---

## 6. 实现困难

### 6.1 物理上限（不可绕过的）
- 低频段天然慢：时频不确定性是物理定律。任何方案在 20~50 Hz 都无法做到"既 0.4 Hz 分辨又 <0.1 s 响应"。
- 只能"接受慢低频"或"放弃细低频"，或"按用途分流"。

### 6.2 低延迟采集（Python 侧最难）
- sounddevice/PortAudio **不原生支持 loopback**，当前靠"立体声混音"虚拟设备（依赖系统音频设置，且混音路径有 SRC 失真）。
- 要真低延迟需 **WASAPI loopback + 小 buffer**（~10 ms），Python 侧得自写 C 扩展或走 `cffi` 包 cpal/CSCore，工程量大。
- 这属于"锦上添花"：当前 21 ms 输入块已不是主要矛盾（FFT 窗才是）。

### 6.3 两级 FFT 拼接
- crossover 处两段曲线不连续 → 需重叠过渡（如 400~600 Hz 线性混合）。
- 两套 FFT 的窗长不同，低频 bin 的高斯/汉宁旁瓣不同，拼接边界可能有台阶。
- 维护两套 `_projection` / `_bin_polygon` 索引，代码复杂度上升。

### 6.4 平滑的取舍
- 不拖沓需要低平滑，但低频单 bin 逐帧抖动会更明显（这正是当初用 EMA 的原因）。
- 折中：attack 快（如 0.8）/ release 慢（如 0.3）的**非对称动态**，比单一 EMA 更"跟手且稳"。

### 6.5 CPU 预算
- 65536 FFT 每回调 ~3–5 ms（47 次/s → 单核 ~20%）。
- 若上双 FFT（方案 B/D）：再 +4096 FFT（~0.2 ms），总体仍可控；但 180 FPS 渲染 + FFT 双线程，需留意风扇噪音。
- numpy rfft 在音频回调线程同步执行，若 CPU 被打满有阻塞/欠载风险；极端情况需把 FFT 移到工作线程 + ring buffer（行业标准做法）。

### 6.6 文档/回归
- README、模块 docstring 与代码多处已漂移（FFT_SIZE、更新率、dB 域注释），需一并校准。
- 纯逻辑测试（`tests/`）要随 `clamp_db`/缩放函数变更回归。

---

## 7. 结论与建议

1. **拖沓的根因 = FFT_SIZE=65536 的长窗（1.37 s），不是帧率、不是 Python 慢、不是渲染。**
2. **mineradio 与行业标准的答案一致**：FFT 2048–4096（几十 ms 窗）+ 频段聚合 + 音频线程原生分析。8cava 反向选择大窗，换来了拖沓。
3. **C++ 改写不能解决**：numpy FFT 底层即 C，瓶颈是窗长这一算法决策；C++ 只降 CPU%。
4. **建议实施路径**：方案 A（快改验证）→ 需要时上方案 B（两级 FFT）+ D（显示/录制分流）。
5. 若最终目标是"像 mineradio 一样无延迟的频谱"，**核心动作是：小窗（≤4096）+ 分频段/感知聚合 + 显示与录制分流**；低频 8px 与实时跟手二者只能保其一（或分用途各保）。

---

## 附录：参考来源

- mineradio 源码（本机）：`D:\Program Files\mineradio\body2.1.0\Mineradio\resources\app\public\js\modules\`
  - `00-state/00-core-stores.js`（FFT_SIZE=2048）
  - `05-playback/08-audio-graph-controls.js`（双 Analyser + smoothingTimeConstant）
  - `11-main-loop.js`（rAF 拉 getByteFrequencyData）
  - `03-beat/06-sonic-audio-monitor.js`（512-bin 频段聚合、8 感知频段）
- 8cava：`8cava/spectrum.py`（FFT_SIZE=65536 等参数）
- 行业：
  - [SpectrumNet (C# WASAPI loopback)](https://github.com/diqezit/SpectrumNet)
  - [SpectrumCpp (C++/WASAPI/Direct2D，热键切 FFT 窗)](https://github.com/diqezit/SpectrumCpp)
  - [BeSpec (Rust/cpal，固定 2048 FFT)](https://github.com/bespec-dev/bespec)
  - [aiXander/Realtime_PyAudio_FFT（8–15 ms 端到端、零分配回调）](https://github.com/aiXander/Realtime_PyAudio_FFT)
  - [Stack Overflow — WASAPI loopback 频谱（CSCore，FFT4096 典型值）](https://stackoverflow.com/revisions/c0389dd9-174c-4fb9-b223-b926bb557e55)
  - [基于 Irrlicht + WASAPI 的频谱分析（ring buffer + 工作线程）](https://www.cnblogs.com/sharpeye/p/19817922)
  - [DIY Audio — FFT of digital loopback 讨论（窗长与伪影、WASAPI exclusive/ASIO）](https://www.diyaudio.com/community/threads/fft-of-digital-loopback.420045/)
