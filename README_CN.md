# screenstudio-export

免订阅导出 [Screen Studio](https://www.screen.studio/) 录屏项目为 MP4 视频。

Screen Studio 的录制文件是完全公开的格式（JSON 配置 + 标准 MP4 视频分段）。编辑功能免费，但导出需要付费订阅。本工具直接读取项目数据，渲染出包含所有编辑效果的最终视频。

## 功能状态

### 已实现

| 功能 | 说明 |
|------|------|
| 缩放 — 手动定点 | 缩放到指定位置，支持配置 `manualTargetPoint` (x, y) |
| 缩放 — 跟随鼠标 | 视口跟随光标位置，带死区防抖 |
| 缩放 — 跟随点击 | 视口跟随最近一次点击位置 |
| 变速播放 | 每个片段独立 `timeScale`，支持慢放到 24 倍快进，支持片段剪切 |
| 弹簧物理动画（视口） | 缩放和平移过渡使用弹簧模拟器（质量、刚度、阻尼），动画丝滑自然 |
| 弹簧物理动画（光标） | 光标移动独立弹簧平滑，不受原始数据抖动影响 |
| 输出时间轴弹簧 | 弹簧动画在输出时间轴上模拟，快进片段中缩放过渡不会被加速压缩 |
| 光标渲染 | 使用录制的原始光标图片，正确的热点对齐，可配置大小倍率 |
| 光标自适应缩放 | 光标大小根据输出分辨率自动调整 |
| 运动模糊 | 视口移动/缩放时多帧混合产生运动模糊，子帧数可配置 |
| 多 Session 支持 | 自动处理任意数量的录制分段 |
| 时间轴映射 | 精确的输出时间到源时间映射，跨片段和 Session |
| 硬件编码 | macOS VideoToolbox H.264 硬件加速，支持 libx264 软件回退 |

### 待实现

| 功能 | 优先级 | 配置字段 | 说明 |
|------|--------|----------|------|
| **背景画布** | 高 | `backgroundType`, `backgroundGradient`, `backgroundImage`, `backgroundSystemName`, `backgroundBlur`, `backgroundColor` | 当 `backgroundPaddingRatio > 0` 时，视频缩小显示在渐变/壁纸/图片背景上 |
| **背景内边距** | 高 | `backgroundPaddingRatio`, `insetPadding` (上下左右), `insetColor`, `insetAlpha` | 录制画面周围的画布区域 |
| **窗口圆角** | 高 | `windowBorderRadius` | 录制画面的圆角效果 |
| **窗口阴影** | 中 | `shadowIntensity`, `shadowAngle`, `shadowDistance`, `shadowBlur`, `shadowIsDirectional` | 录制窗口的投影效果 |
| **摄像头画中画** | 中 | `hideCamera`, `mirrorCamera`, `cameraRoundness`, `cameraSize`, `cameraPosition`, `cameraPositionPoint`, `cameraScaleDuringZoom`, `cameraAspectRatio` | 摄像头画中画叠加层，需要录制数据中有摄像头视频通道 |
| **音频 — 系统声音** | 中 | `audioVolume`, `muteSystemAudio` | 系统音频轨道混合 |
| **音频 — 麦克风** | 中 | `muteMicrophone`, `improveMicrophoneAudio`, `microphoneInStereoMode` | 麦克风轨道混合，可选音频增强 |
| **音频 — 外部设备** | 低 | `muteExternalDeviceAudio` | 外部设备音频混合 |
| **音频 — 背景音乐** | 低 | `backgroundAudioFileName`, `muteBackgroundAudio`, `backgroundAudioVolume` | 叠加背景音乐文件 |
| **音频 — 逐片段音量** | 低 | slice `volume`, `systemAudioVolume`, `externalDeviceAudioVolume` | 每个时间片段独立音量控制 |
| **点击音效** | 低 | `clickSoundEffect`, `clickSoundEffectVolume` | 鼠标点击时的音频反馈 |
| **点击视觉特效** | 低 | `clickEffect` | 点击时的涟漪/高亮动画 |
| **快捷键叠加层** | 中 | `showShortcuts`, `hiddenShortcuts`, `showShortcutsWithSingleLetters`, `shortcutsSizeRatio` | 浮动 UI 显示按下的快捷键组合 |
| **字幕/转录** | 低 | `showTranscript`, `transcriptSizeRatio` | 语音转文字字幕叠加 |
| **设备外壳** | 低 | `deviceFrameKey`, `enableDeviceMockup`, `adjustDeviceFrameToRecordingSize` | MacBook/iPhone/iPad 外壳包裹录制画面，素材在 Screen Studio 应用包内 |
| **录制裁剪** | 低 | `recordingCrop` (x, y, width, height 归一化) | 裁剪源录制的子区域 |
| **滑动动画** | 低 | zoomRange `glideDirection`, `glideSpeed` | 缩放区间内的缓慢平移运动 |
| **瞬间缩放** | 低 | zoomRange `hasInstantAnimation` | 跳过弹簧动画直接切换缩放 |
| **保持缩放** | 低 | `alwaysKeepZoomedIn` | 缩放区间之间不回到全景 |
| **光标自动隐藏** | 低 | `hideNotMovingCursorAfterMs` | 光标静止一段时间后自动隐藏 |
| **光标去抖** | 低 | `removeCurshorShakeTreshold` | 过滤光标的微小抖动 |
| **光标循环** | 低 | `loopCursorPositionBeforeEndMs` | 录制末尾光标位置循环 |
| **逐片段隐藏光标** | 低 | slice `hideCursor` | 特定时间片段隐藏光标 |
| **逐片段平滑开关** | 低 | slice `disableSmoothMouseMovement` | 特定片段禁用光标弹簧平滑 |
| **布局系统** | 低 | `defaultLayout`, scene `layouts` | 多源布局排列 |
| **遮罩** | 低 | scene `masks` | 区域遮罩/模糊 |
| **边缘吸附** | 低 | zoomRange `snapToEdgesRatio` | 视口接近屏幕边缘时自动吸附 |
| **输出宽高比** | 低 | `defaultOutputAspectRatio` | 强制指定宽高比，添加黑边 |

## 项目文件格式

```
MyRecording.screenstudio/
  project.json            # 编辑配置：缩放区间、变速片段、弹簧参数、光标、背景等
  meta.json               # Screen Studio 版本信息
  recording/
    metadata.json          # Session 边界、录制器类型、显示器尺寸
    channel-1-display-0.mp4     # 视频初始化段（分片 MP4，H.264）
    channel-1-display-0-*.m4s   # 视频媒体分段
    channel-1-display-0.m3u8    # HLS 播放列表（本工具不使用）
    channel-1-display-1.mp4     # 额外 Session 视频（多段录制时）
    cursors.json           # 光标元数据：id、hotSpot {x,y}、standardSize {w,h}
    cursors/               # 光标 PNG 图片（arrow.png、iBeam.png、pointingHand.png 等）
    mousemoves-0.json      # 鼠标位置事件：{x, y, processTimeMs, cursorId}
    mouseclicks-0.json     # 点击事件：{x, y, processTimeMs, button, type}
    keystrokes-0.json      # 按键事件：{character, activeModifiers, processTimeMs}
    metadata-raw.json      # 原始录制元数据
    polyrecorder.log       # 录制引擎日志
```

## 环境要求

- Python 3.9+
- [FFmpeg](https://ffmpeg.org/)（需在 PATH 中）
- [Pillow](https://pillow.readthedocs.io/)
- [NumPy](https://numpy.org/)（可选，提升运动模糊性能）

## 安装

```bash
git clone https://github.com/shaominngqing/screenstudio-export.git
cd screenstudio-export
pip install -r requirements.txt
```

## 使用方法

```bash
# 基本用法 — 自动输出到项目同级目录
python screenstudio-export.py "E:\ScreenStudio\第三期-成员展示.screenstudio"  --nvenc --nvdec --workers 8
python screenstudio-export.py "E:\ScreenStudio\第三期-调度中心展示.screenstudio"  --nvenc --nvdec --workers 8
python screenstudio-export.py "E:\ScreenStudio\第三期-001.screenstudio"  --nvenc --nvdec --workers 8
python screenstudio-export.py "E:\ScreenStudio\第三期-005.screenstudio"  --nvenc --nvdec --workers 8
python screenstudio-export.py "E:\ScreenStudio\第三期-核验最终功能.screenstudio"  --nvenc --nvdec --workers 8
python screenstudio-export.py "E:\ScreenStudio\第三期-最终决策-预览.screenstudio"  --nvenc --nvdec --workers 8
python screenstudio-export.py "E:\ScreenStudio\第三期-素材NotebookLM.screenstudio"  --nvenc --nvdec --workers 8

# 指定输出路径和分辨率
python screenstudio-export.py "我的录制.screenstudio" -o output.mp4 --width 1920 --height 1080

# 4K 60fps 导出
python screenstudio-export.py "我的录制.screenstudio" -o 4k.mp4 --width 3840 --height 2160 --fps 60 --bitrate 20M

# 快速渲染（关闭运动模糊）
python screenstudio-export.py "我的录制.screenstudio" --no-motion-blur

# Linux 或无硬件编码器
python screenstudio-export.py "我的录制.screenstudio" --software-encoder
```

## 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `-o, --output` | `<项目名>.mp4` | 输出文件路径 |
| `--fps` | `60` | 输出帧率 |
| `--width` | `1920` | 输出宽度（像素） |
| `--height` | `1080` | 输出高度（像素） |
| `--deadzone` | `160` | 跟随模式死区像素（减少画面抖动） |
| `--blur-subframes` | `7` | 运动模糊子帧数（1 = 关闭） |
| `--bitrate` | `12M` | 视频码率 |
| `--no-cursor` | — | 隐藏光标 |
| `--no-motion-blur` | — | 关闭运动模糊 |
| `--software-encoder` | — | 使用 libx264 代替 macOS VideoToolbox 硬件编码 |

## 工作原理

1. 解析 `project.json`，提取缩放区间、变速片段和弹簧参数
2. 根据片段定义构建输出时间到源时间的映射
3. 在**输出时间轴**上模拟弹簧物理（缩放动画不受变速影响）
4. 通过 FFmpeg 子进程管道解码源视频帧
5. 根据弹簧动画状态对每帧进行视口裁剪和缩放
6. 视口移动时混合多个子帧产生运动模糊
7. 在正确的屏幕位置合成光标
8. 通过 FFmpeg 编码输出（macOS 上使用硬件加速）

## 免责声明

本项目是一个独立工具，读取公开结构的项目文件（纯 JSON 和标准 MP4 视频）。不包含、不逆向工程、不分发 Screen Studio 的任何源代码、二进制文件或专有资源。

Screen Studio 是一款优秀的产品 — 如果你经常使用，请考虑[购买订阅](https://www.screen.studio/)支持开发者。本工具仅供个人和学习使用。

## 贡献

Screen Studio 的项目格式完全公开（纯 JSON + 标准视频文件）。所有未实现功能的配置字段已在上方表格中记录。欢迎 PR！

## 许可证

MIT
