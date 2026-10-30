# translator

多人游戏内实时翻译：**看懂屏幕、听懂队友**。

适用于 FPS（CS2/Apex/瓦罗兰特）、MOBA（Dota 2/LoL）、英雄射击（守望先锋）等
需要跨语言沟通的竞技游戏。

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

每一环都是 `translator.core.interfaces` 里的抽象基类，通过 `translator.core.registry`
按名字注册和创建，因此任何一环都能被配置文件替换掉。

```
src/translator/
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
python -m translator doctor

# 生成默认配置
python -m translator config

# 截一次屏，看看 OCR 能读出什么（调 ROI 用这个）
python -m translator scan --region 0.02 0.55 0.5 0.4

# 测试（-m 'not slow' 跳过加载真模型的用例）
python -m pytest -q
```

Windows 机器上建议 Python **3.11**（faster-whisper / PySide6 的轮子最全），
用 `uv` 或官方安装包均可。

---

## 打包给 Windows 用

**PyInstaller 不能交叉编译**：在 Linux/macOS 上跑只能得到本平台的二进制。要 Windows
的 `.exe`，必须在 Windows 上构建。两条路，任选：

### 路线 A：让仓库自动构建（推荐，不需要本地环境）

1. 把仓库推到 GitHub
2. 打开 **Actions → Windows build → Run workflow**

构建完成后在该次运行的 **Artifacts** 里下载 `Translator-Setup`，那是一个可直接安装的
`Translator-Setup.exe`。

打 tag 会自动发 Release：

```bash
git tag v0.1.0
git push --tags          # 之后在 Releases 页面直接下载
```

### 路线 B：在 Windows 上本地构建

需要 Windows 上装 **Python 3.11 或 3.12**，然后：

```powershell
# 一键构建（自动建 venv、装依赖、跑 PyInstaller）
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1

# 想要单个安装包，再装 Inno Setup 6 后执行
powershell -ExecutionPolicy Bypass -File packaging\make_installer.ps1
```

产物：

| 路径 | 内容 |
|---|---|
| `dist\Translator\Translator.exe` | 免安装版，整个目录可拷走 |
| `dist\installer\Translator-Setup.exe` | 安装包（LZMA 压缩，约 130 MB）|

### 体积构成（实测，Linux 版同构）

| 组件 | 大小 | 说明 |
|---|---|---|
| OpenCV（headless）| 152 MB | rapidocr 的硬依赖，含 BLAS 和编解码库 |
| PySide6 | 100 MB | 已剔除 WebEngine/Quick/3D 等 |
| onnxruntime | 30 MB | OCR 推理 |
| numpy | 41 MB | |
| rapidocr 模型 | 16 MB | det + rec + cls |

安装包经 LZMA 压缩后约 **130 MB**。构建脚本会**主动把 opencv-python 换成
opencv-python-headless**——rapidocr 默认拉的是 GUI 版，会白带一整套私有 Qt5（约 30 MB）。

### 装完之后

首次启动会弹向导：先填翻译凭据，再拖框选游戏里的聊天区域。凭据存进 **Windows 凭据管理器**
（DPAPI 加密），不落配置文件。

出问题看日志：`%APPDATA%\Translator\logs\translator.log`

---

## 界面

`translator run` 启动的是完整应用：**托盘图标 + 透明字幕 + 设置窗口**。

```
托盘（游戏中唯一能操作的入口）
├── 暂停 / 启动          图标颜色 = 状态
├── 重新框选区域…         绿=运行 黄=暂停 灰=停止 红=出错
├── 设置…
├── 设置密钥…
└── 退出

设置窗口（双击托盘图标）
├── 扫描区域   区域列表、重新框选、删除单个区域
├── 翻译       引擎/模型/接口地址、密钥状态、术语表表格
├── 外观       字号/位置/停留时长/淡入/描边/底板 + 实时预览
├── 性能       帧率/OCR 间隔/线程数/识别高度
└── 状态       已翻译条数、OCR 与翻译耗时、缓存命中率、错误
```

为什么必须有托盘：游戏全屏时没人愿意切窗口，启停和改配置只能从托盘走。
设置窗口编辑的是配置的**副本**，所以「取消」永远不会破坏正在运行的状态。

没有系统托盘的环境（某些 Linux 桌面）会自动退化为「只有字幕」模式。

---

## 配置翻译服务（唯一需要密钥的地方）

**密钥是启动的硬性前提**——没有密钥不允许运行（`engine = "mock"` 除外，那是调试开关）。
首次运行 `translator watch` 会直接提示输入，校验通过后才开始工作：

```bash
translator auth set        # 交互式输入，自动校验并存进系统凭据管理器
translator auth status     # 查看（掩码显示）和来源
translator auth clear      # 删除
```

存储优先级（第一个命中的生效）：

| 顺序 | 位置 | 说明 |
|---|---|---|
| 1 | 环境变量 `TRANSLATOR_API_KEY` | CI / 高级用户 |
| 2 | **系统凭据管理器** | Windows 凭据管理器（DPAPI 加密，绑定用户账户）|
| 3 | `config.toml` | 兼容用，会打印警告 |

不写进配置文件是有意的：明文 key 一旦截图或发到群里就泄露了。

```bash
export TRANSLATOR_API_KEY="sk-..."        # Windows: setx TRANSLATOR_API_KEY "sk-..."
```

本地模型（Ollama/LM Studio，`localhost`/`127.0.0.1`）**不要求密钥**，自动跳过。

### 两路引擎可选

| 引擎 | 优点 | 代价 |
|---|---|---|
| `llm`（默认）| 游戏黑话翻得准，术语表能进提示词 | 需要联网调模型，按 token 计费 |
| `baidu` | 便宜、国内直连、稳定 | 传统机翻，`rush` 会译成"冲"而不是"快攻" |

```toml
[translate]
engine = "auto"     # auto 优先 llm，没有 key 时用 baidu；也可写死 "llm" / "baidu"

# --- engine = "baidu" 时用 ---
baidu_appid = ""            # 建议用环境变量 TRANSLATOR_BAIDU_APPID
baidu_key = ""              # 建议用环境变量 TRANSLATOR_BAIDU_KEY
baidu_qps = 1.0             # 免费版是 1 QPS，超了会返回 54003
```

百度凭据同样存进系统凭据管理器：

```bash
translator auth set        # 按当前 engine 提示输入（百度要 APPID + 密钥）
translator auth --verify   # 实测一次，确认签名正确
```

**OpenAI 兼容端点**（`engine = "llm"`）只改 `base_url` + `model`：

```toml
[translate]
engine = "llm"
base_url = "https://api.deepseek.com/v1"
model = "deepseek-flash"     # 快，适合字幕；deepseek-v4-pro 会推理，不要用
# 通义: https://dashscope.aliyuncs.com/compatible-mode/v1   model = "qwen-plus"
# Kimi: https://api.moonshot.cn/v1                         model = "moonshot-v1-8k"
# 本地 Ollama（无需密钥）: http://localhost:11434/v1       model = "qwen2.5:7b"
```

**为什么用 LLM 而不是传统机翻**：游戏黑话是机翻重灾区（`rush` → 「冲」而不是
「快攻」，`eco` → 「生态」）。术语表会作为提示词注入：

```toml
# 在默认术语表之上追加（推荐：不会丢掉内置的 gg/thx/oom 等）
[translate.glossary_extra]
"tp" = "传送"
"deny" = "反补"
peek = "探头对枪"

# 完全替换内置术语表（谨慎用）
# [translate.glossary]
```

**按游戏定制语境**——同一句黑话在不同品类里意思不同（`push` 在 Dota 是推塔，
在 CS 是压上）。`context` 会覆盖通用规则的优先级：

```toml
[translate]
context = "Dota 2. 'tp' = 传送. 'deny' = 反补. 保留英雄名原文."
# context = "Overwatch. 'ult' = 终极技能. 保留英雄名原文: Tracer, Winston."
```

**批量是必须的，不是优化**：屏幕上同时出现的 4 行会合并成 1 次请求。LLM 每次调用都
有往返和 token 成本，逐行调用会贵 4 倍、慢 4 倍。

**失败策略**：模型返回坏 JSON、条数不对或超时，都退回**原文**而不是丢掉这一行 ——
看到英文也比什么都没有强，字幕不会断档。

### 必须关掉推理（已默认处理）

DeepSeek V4 默认会「先思考再回答」。同一份请求跑 5 次实测：

| 参数 | 触发推理 | 中位延迟 | 空响应 |
|---|---|---|---|
| 默认 | 3/5 | 1483 ms | 偶发 |
| `effort = "low"` | 5/5（**更糟**） | 1536 ms | — |
| `reasoning_effort = "none"` | 0/5 | 1189 ms | 0 |
| **`thinking = {type: "disabled"}`** | **0/5** | **880 ms** | **0** |

推理对我们是有害的：它吃掉 `max_tokens`，实测出现过整个预算被思考耗尽、`content`
返回空字符串的情况——**等于没有字幕**。而且 `temperature = 0` 挡不住它。

代码已默认对 `deepseek*` 模型发送 `thinking = {type: "disabled"}`。如果你的端点不认
这个字段，设 `disable_thinking = false`；其他 provider 需要额外参数时用
`extra_params`。

---

## 资源预算（硬约束）

本产品的定位是**游戏旁边的隐形字幕**，不是要抢 CPU。所有实现取舍都服从这张表，
由 `translator bench` 强制检查：

| 资源 | 预算 | 当前实测（16 线程 / Ryzen 4800U） |
|---|---|---|
| CPU | **≤ 50% 单核**（最坏情况） | **23%** = 整机 1.5% |
| 内存 | ≤ 400 MB | 136 MB（OCR 模型占 ~110MB） |
| GPU | 0（不用 CUDA/DML） | 0 |
| 磁盘 | 稳态 0（只在启动加载模型） | 0 |

```bash
python -m translator bench --region 0.0 0.62 0.36 0.16   # 在你机器上验证
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
