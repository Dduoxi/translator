# fps-translator

FPS 游戏内实时翻译：**看懂屏幕、听懂队友**。

本期范围（明确的非目标）：

| 方向 | 本期 | 说明 |
|---|---|---|
| 屏幕英文字幕/聊天 → 中文 | ✅ 做 | DXGI 截屏 + OCR + 翻译 → 置顶 overlay |
| 队友麦克风语音 → 中文字幕 | ✅ 做 | WASAPI 进程级回环 + VAD + ASR + 翻译 |
| 我的中文 → 英文语音进游戏 | ❌ 不做 | 需要内核级虚拟麦克风驱动，另立项目 |

设计红线：**只截图、只抓音频，绝不注入游戏进程、不 hook、不逆向语音协议。**

---

## 架构

```
ScreenSource ──▶ OCREngine ─────────┐
   (mss/dxgi)     (rapidocr)        │
                                    ├──▶ Translator ──▶ Subtitle ──▶ Overlay
AudioSource ──▶ VoiceSegmenter ──▶ ASREngine ─┘        (Qt 透明置顶)
(process loopback)    (VAD)      (faster-whisper)
```

每一环都是 `fpst.core.interfaces` 里的抽象基类，通过 `fpst.core.registry`
按名字注册和创建，因此任何一环都能被配置文件替换掉。

```
src/fpst/
├── core/        types, bus, config, registry, interfaces  ← 无第三方依赖，跨平台
├── capture/     屏幕源
├── ocr/         文字识别
├── audio/       音频源 + VAD 切句
├── asr/         语音识别
├── translate/   翻译
└── ui/          Qt overlay
```

`core/` 与 `builtin.py` 必须在 Linux 和 Windows 上都能 import —— 平台专属代码
只在具体引擎模块里、且延迟导入。

---

## 开发流程（双机协作）

这台 Linux 机器上能跑：`core/`、OCR、翻译、overlay 的界面布局、pytest。
必须到 Windows 上验：进程级音频回环、DXGI 捕获、驱动、反作弊兼容性。

```bash
# 一次性准备
uv venv --python 3.11
uv pip install -e ".[dev,screen,ocr,translate,ui]"

# 环境体检（Windows 上先跑这个）
python -m fpst doctor

# 生成默认配置
python -m fpst config

# 截一次屏，看看 OCR 能读出什么（调 ROI 用这个）
python -m fpst scan --region 0.02 0.55 0.5 0.4

# 测试（-m 'not slow' 跳过加载真模型的用例）
python -m pytest -q
```

Windows 机器上建议 Python **3.11**（faster-whisper / PySide6 的轮子最全），
用 `uv` 或官方安装包均可。

---

## 资源预算（硬约束）

本产品的定位是**游戏旁边的隐形字幕**，不是要抢 CPU。所有实现取舍都服从这张表，
由 `fpst bench` 强制检查：

| 资源 | 预算 | 当前实测（16 线程 / Ryzen 4800U） |
|---|---|---|
| CPU | **≤ 50% 单核**（最坏情况） | **23%** = 整机 1.5% |
| 内存 | ≤ 400 MB | 136 MB（OCR 模型占 ~110MB） |
| GPU | 0（不用 CUDA/DML） | 0 |
| 磁盘 | 稳态 0（只在启动加载模型） | 0 |

```bash
python -m fpst bench --region 0.0 0.62 0.36 0.16   # 在你机器上验证
```

### 成本模型

实测：**CPU ≈ 130 ms 固定开销 + 185 ms/行**（1 线程，rec 高度 48）。
所以真正的成本驱动因素是 **ROI 里有几行字**，不是 ROI 多大。

| ROI 内的行数 | CPU/次 | 1.5s 间隔 → 单核占用 |
|---|---|---|
| 1 | 254 ms | 17% |
| 3 | 569 ms | 38% |
| 5 | 831 ms | 55%（超预算） |
| 8 | 1471 ms | 98%（超预算） |

**所以 `screen.region` 要裁到只包住聊天框的文字区，3 行以内。**

### 四个关键取舍（都是拿「延迟」换「CPU」）

| 手段 | 收益 | 代价 |
|---|---|---|
| `[ocr] threads = 1` | CPU 降 **4.3x**（3325→781ms/次） | 墙钟慢 1.9x |
| `[ocr] rec_height = 32` | CPU 降 **2.3x**（922→399ms） | 精度不变（实测 4/4 行全对） |
| `[performance] ocr_min_interval = 1.5` | 平均 CPU 再降 | 字幕延迟 +1.5s 上限 |
| `[performance] low_priority = true` | 游戏永远能抢占我们 | 满载时我们的 OCR 会变慢 |

1 线程 + 低延迟不是矛盾：**多线程是拿游戏的 CPU 换我们自己的响应速度**，这个交易
对后台工具是错的。宁可慢 300ms，也不能抢核。

优先级降低（`BELOW_NORMAL`）是最后一道保险：即使我们算错了预算，游戏也不会掉帧。

---

## 实测性能（Ryzen 4800U，16 线程，无独显）

OCR 用的 PP-OCRv4 mobile（det + rec）。三个反直觉的结论：

| 配置 | 耗时 | 说明 |
|---|---|---|
| 全部默认（16 线程 + batch 6） | 815 ms | onnxruntime 默认值在这里是最差的 |
| 8 线程 + batch 6 | 658 ms | det=75ms, rec=564ms |
| **8 线程 + batch 1** | **430 ms** | 当前默认 |

1. **线程数不能给满**。`intra_op_num_threads=16` 比 `8` 慢一倍，这些 mobile 模型
   太小，线程调度开销盖过并行收益。默认取 `cpu_count//2`，上限 8。
2. **`rec_batch_num` 必须是 1**。批处理会把每行 padding 到最长行的宽度，一句 44
   字符的消息能让另外三行短消息的计算量翻 4 倍。
3. **det 不是瓶颈，rec 是**（75ms vs 564ms）。所以真正的优化杠杆不是「让 OCR 更快」，
   而是「不跑 OCR」—— 见 `capture/gate.py` 的 `ChangedOnly`。

ROI 大小是线性成本：全屏 1920x1080 有 78 个文本框，要 **5 秒**；裁到
`864x540` 的 22 个框，1.4 秒。所以 `screen.region` 不是可选项，是必须配的。

---

## 关键技术约束

**进程级音频回环**是「零安装」的核心。Windows 10 build 19041+ 提供
`ActivateAudioInterfaceAsync` + `AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK`，
可以只捕获指定进程（含子进程树）的输出，而不必抓整个扬声器混音 —— 这样
枪声和 BGM 就不会污染 ASR。

**屏显文字的 ROI** 以相对坐标（0..1）存储，换分辨率不失效。

**OCR 降频**：FPS 里聊天框绝大多数时间是静止的，帧间差分低于阈值就直接跳过
OCR，热路径成本降一个数量级。
