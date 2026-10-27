# Edge TTS to BlackHole

使用 Edge TTS 合成中文男声，自动读取 macOS `BlackHole 2ch` 当前的采样率，
并将音频转换到相同采样率后写入该设备。会议软件或录音软件将麦克风切换为
`BlackHole 2ch` 后，即可收到合成语音。

程序会等待设备采样率稳定后再播放，并在流式播放期间持续监控采样率。如果其他
应用重新协商并改变了 BlackHole 的采样率，程序会停止推送，避免产生变速失真。

## 环境

- macOS，并已安装 `BlackHole 2ch`
- Python 3.10+ 和 [uv](https://docs.astral.sh/uv/)
- FFmpeg：`brew install ffmpeg`

首次运行时，uv 会根据 `pyproject.toml` 自动安装依赖：

```bash
uv sync
```

## macOS BlackHole 安装与配置

### 1. 安装 BlackHole 2ch

已安装 [Homebrew](https://brew.sh/) 时，在终端执行：

```bash
brew install --cask blackhole-2ch
brew install ffmpeg uv
```

也可从 [BlackHole 官网](https://existential.audio/blackhole/) 下载 2ch 安装包。
安装完成后重启 Mac，再打开会议软件和终端。BlackHole 是虚拟音频驱动，
没有需要单独启动的应用窗口。安装参考：[官方说明](https://github.com/ExistentialAudio/BlackHole#installation-instructions)。

### 2. 检查设备与采样率

1. 按 `Command+空格` 搜索并打开“音频 MIDI 设置”（Audio MIDI Setup）。
2. 若未显示设备列表，选择“窗口 > 显示音频设备”。
3. 确认列表里出现 `BlackHole 2ch`，输入和输出均为双声道。
4. 可先将格式设为 `48,000 Hz`；脚本会读取设备的实际采样率并转换音频，
   无需将其固定为 Edge TTS 的源采样率。播放过程中不要手动改变格式。

在项目目录安装依赖并列出音频设备：

```bash
uv sync
uv run python -m sounddevice
```

### 3. 配置会议软件和权限

- 腾讯会议、Teams 等应用：麦克风选择 `BlackHole 2ch`，扬声器选择实体耳机
  或 Mac 扬声器。建议使用耳机，避免实体麦克风再次拾取会议声音。
- macOS 系统默认输出保持实体耳机或扬声器即可，不必切换到 BlackHole。
- 使用 `mix`、`ptt` 或回录验证时，在“系统设置 > 隐私与安全性 > 麦克风”
  中允许运行脚本的终端或 IDE；修改权限后重新启动该程序。
- `ptt` 模式还需要输入监控权限，详见下方使用说明。

基础路由：`控制台文字 → Edge TTS → BlackHole 2ch → 会议软件麦克风`。
`mix` / `ptt` 模式还会由脚本把实体麦克风混入同一路输出，无需创建聚合设备。

### 4. 验证与排查

先退出占用 BlackHole 的会议软件，再执行独立回录测试：

```bash
uv run tts_to_blackhole.py --verify "你好，这是 BlackHole 安装验证。"
```

检查日志中的回录信号是否为非静音，再启动会议软件，运行
`uv run tts_to_blackhole.py --interactive console`，输入文字并观察会议软件
的麦克风电平或录制测试。直接输出到 BlackHole 时，本机扬声器听不到声音是正常的。

- 找不到设备：确认安装的是 `2ch` 版本，重启 Mac，再运行设备列表命令。
- 实体麦克风找不到：从设备列表复制实际名称，通过 `--mic-device "设备名称"`
  指定；不同机型和系统语言的名称可能不同。
- 会议中没有声音：检查应用的麦克风选择、静音状态及权限；必要时关闭降噪、
  自动增益等处理后复测。
- 出现采样率变化或声音变速：先打开会议软件并选好设备，等待格式稳定后再启动
  脚本；若脚本因采样率变化停止，稳定后重新运行。

## 使用

### 播放本地音频文件到麦克风

将会议软件的麦克风设为 `BlackHole 2ch`，然后执行：

```bash
uv run tts_to_blackhole.py --file "/路径/录音.mp3"
```

支持 FFmpeg 可解码的音频格式，如 MP3、WAV、M4A、FLAC。文件模式无需调用
Edge TTS，按正常速度播放一次，自动转换到输出设备采样率和双声道；播放结束退出，
可按 `Ctrl+C` 中止。路径包含空格时请加引号。

可指定输出设备，或在退出会议软件后单独验证 BlackHole 回录：

```bash
uv run tts_to_blackhole.py --file "./录音.wav" --device "BlackHole 2ch"
uv run tts_to_blackhole.py --file "./录音.wav" --verify
```

`--file` 不可与朗读文本、`--interactive`、`--stream` 混用；此模式不混入实体
麦克风。`--voice`、`--rate`、`--volume` 为 TTS 参数，不改变文件播放。
不支持的格式或损坏文件会由 FFmpeg 报错；`--verify` 的会议使用限制见下文。

### 交互模式

仅接收控制台文字，不读取物理麦克风：

```bash
uv run tts_to_blackhole.py --interactive console
```

接收控制台文字，同时把实体麦克风持续送入 BlackHole：

```bash
uv run tts_to_blackhole.py --interactive mix
```

按住 F12 时才把实体麦克风送入 BlackHole，松开后立即恢复为仅控制台 TTS：

```bash
uv run tts_to_blackhole.py --interactive ptt
```

`ptt` 使用全局按键监听，因此切换到腾讯会议窗口后仍然有效。首次运行时，需要在
macOS“系统设置 > 隐私与安全性 > 输入监控”中允许当前终端；如果仍然无法监听，
再检查“辅助功能”权限并重启终端。MacBook 默认把 F12 用作音量键时，请按
`Fn+F12`，或在键盘设置中启用标准功能键。

为兼容原来的命令，省略模式时也使用 `mix`：

```bash
uv run tts_to_blackhole.py --interactive
```

在 `mix` 模式下直接说话即可传递物理麦克风声音。三种模式都可以在控制台输入
文字并按回车触发流式 TTS；输入 `/quit` 或按 `Ctrl+C` 退出。
默认使用 `MacBook Pro麦克风`，也可以指定其他物理麦克风并调整两路增益：

```bash
uv run tts_to_blackhole.py --interactive mix \
  --mic-device "MacBook Pro麦克风" \
  --mic-gain 1.0 \
  --tts-gain 0.8
```

较长文本会按自然句段拆分，每段独立校验 Edge TTS 返回的末尾内容，失败时最多重试
3 次。完成日志会显示原文字符数、分段数、音频时长和原文结尾，便于确认尾段已进入
输出队列。Markdown 列表开头的 `*`、`+`、`-` 会作为格式符移除，不会被朗读。

腾讯会议等应用只需将麦克风设置为 `BlackHole 2ch`，不需要选择聚合设备。

### 单次合成

默认使用中文男声 `zh-CN-YunjianNeural`：

```bash
uv run tts_to_blackhole.py "你好，这是虚拟麦克风测试。"
```

边合成边推送，不等待完整 MP3 落盘：

```bash
uv run tts_to_blackhole.py --stream "你好，这是流式虚拟麦克风测试。"
```

播放的同时从 BlackHole 回录，确认虚拟麦克风确实收到非静音信号：

```bash
uv run tts_to_blackhole.py --verify "你好，这是回录验证。"
```

`--verify` 仅用于独立测试。腾讯会议正在使用 BlackHole 时不要启用它，否则脚本
会同时打开 BlackHole 输入端，可能干扰腾讯会议的音频回调。

调整音色和语速：

```bash
uv run tts_to_blackhole.py \
  --voice zh-CN-YunyangNeural \
  --rate=+10% \
  "这是一段中文男声测试。"
```

查看 Edge TTS 可用声音：

```bash
uv run edge-tts --list-voices
```

## 路由说明

脚本按名称直接打开 BlackHole，不会修改 macOS 的默认扬声器。要让腾讯会议、
Teams 等应用收到声音，需要在目标应用里将麦克风设置为 `BlackHole 2ch`。

BlackHole 本身不会把声音同步播放到 MacBook 扬声器。若需要本地监听，可在
“音频 MIDI 设置”左下角点击 `+ > 创建多输出设备`，同时勾选实体输出和
`BlackHole 2ch`，将实体输出设为主设备／时钟源，其他设备启用漂移校正，
并保持采样率一致。将其重命名为 `TTS Monitor` 后，显式让脚本输出到它：

```bash
uv run tts_to_blackhole.py --interactive console --device "TTS Monitor"
```

仅创建多输出设备或修改系统默认输出，不会改变脚本默认直连 BlackHole 的路由。
会议软件的麦克风仍选 `BlackHole 2ch`，扬声器仍选实体输出，避免将远端声音送回
BlackHole 形成回声。多输出设备不能用系统音量键统一调音量，可分别调整实体设备。
配置参考：[BlackHole 官方多输出设备指南](https://github.com/ExistentialAudio/BlackHole/wiki/Multi-Output-Device)。

## 测试

```bash
uv run python -m unittest discover -s tests -v
```

单元测试覆盖缓冲、文本分段及交互参数等逻辑；真实音频路由需要按上述步骤验证。
