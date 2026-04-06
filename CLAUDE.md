# CLAUDE.md

这个 fork 的改动背景和使用笔记，给我自己看的。

原仓库：https://github.com/shaominngqing/screenstudio-export
这个 fork：https://github.com/ayou129/screenstudio-export

## 这东西是什么

Screen Studio 的独立导出器。读 `.screenstudio` 项目（JSON + 分片 MP4），
按项目配置回放所有特效，输出一份新的 MP4。**不订阅也能导出**。

**重要**：整个过程**不会修改原始录像**。脚本只从 `<project>/recording/` 读取，
源文件爱怎么动还怎么动。录制什么样，输出就是什么样 + project.json 里配的特效
（zoom、speed、spring、cursor 等等）。

## 运行环境（这台机器）

- Windows 10 + Python 3.12，venv 在 `.venv/`
- FFmpeg 8.1 在 `C:\ffmpeg\bin`，已加入 PATH
- GPU: RTX 4070 Ti SUPER，CPU: Ryzen 9 9950X
- 走 `h264_cuvid` (NVDEC) + `h264_nvenc` (NVENC) 硬件流水线

## 架构要点：端到端 YUV420p

这是最核心的改动。原版代码是 RGB 管道：

```
NVDEC → NV12 → 转 RGB → compose → 转回 YUV → NVENC
             ↑ CPU 重      ↑ CPU 重
```

两次颜色空间转换纯粹浪费 CPU，导致多 worker 扩展不起来。重写成：

```
NVDEC → YUV420p → compose 三个平面 (Y/U/V) → NVENC
                  （完全不碰 RGB）
```

具体决策：

- **Source 用 bounds 分辨率**（如 2560×1440），而不是视频文件的原生像素（如 4096×2304）。
  Screen Studio 在 Retina 屏上录制是 HiDPI 过采样，bounds 是逻辑尺寸。让 cuvid
  在 GPU 上 `-resize` 到 bounds，Python 这边处理更少的像素。最终输出 1080p，
  这点区别肉眼看不出来，但 compose 快 3 倍。

- **cursor 在 `_load_cursors` 里预计算成 YUV + alpha**，blit 时分别对 Y（全分辨率）
  和 U/V（半分辨率）做 alpha 混合，坐标强制对齐到偶数（4:2:0 要求）。

- **motion blur** 三个平面各自用 `cv2.accumulate` + `cv2.convertScaleAbs`，SIMD 加速。

- **多 worker 模式 (`--workers N`)**：把输出帧范围切 N 段，起 N 个 Python 子进程各自
  渲染到 `.<name>_parts/part_NNN.mp4`，最后 `ffmpeg -f concat -c copy` 拼起来。
  每个 worker 重新跑一遍 `simulate_springs()`（从 frame 0 起，确定性结果），
  保证跨 chunk 的动画状态一致。

- **`--chunk START:END`** 是内部用的 flag，master 通过它告诉 worker 渲染哪段。

## 性能实测（这台机器，17 分钟 1080p60 素材，全开特效）

| 版本 | 耗时 |
|---|---|
| 原版 Pillow + LANCZOS | Windows 跑不起来（videotoolbox 写死 macOS） |
| cv2 + RGB pipeline + 单 worker | ~16.5 分钟 |
| cv2 + RGB pipeline + 4 worker | ~7.9 分钟 |
| **cv2 + YUV pipeline + 4 worker + NVENC CQ 18（当前）** | **~5.9 分钟** |

CapCut 同样的活大约 2–3 分钟。再想追上得把 compose 也搬到 GPU shader
（CUDA-OpenCV 或者 PyCUDA 手写 kernel），工作量大，暂时不做。

## 编码质量（为平台二次压缩留余量）

抖音/TikTok 上传后会再压一次，源质量不留余地就会糊。默认 NVENC 参数：

```
-c:v h264_nvenc -preset p6 -tune hq -profile:v high
-rc vbr -cq 18 -b:v 0 -maxrate 60M -bufsize 120M
-spatial-aq 1 -temporal-aq 1 -rc-lookahead 20
```

关键是 `-cq 18`，1080p 下视觉近无损，屏幕录制通常 20–40 Mbps 平均码率。

## 日常用法

```bash
# 标准：最快 + 最高画质（推荐）
python screenstudio-export.py "xxx.screenstudio" --nvenc --nvdec --workers 4

# 留资源给其他程序
python screenstudio-export.py "xxx.screenstudio" --nvenc --nvdec --workers 2

# 快速预览（砍掉 motion blur）
python screenstudio-export.py "xxx.screenstudio" --nvenc --nvdec --workers 4 --no-motion-blur

# 显式输出路径（项目名带 Windows 非法字符如 ':' 时必须指定）
python screenstudio-export.py "xxx.screenstudio" --nvenc --nvdec --workers 4 -o out.mp4
```

关键 flag：

| Flag | 作用 |
|---|---|
| `--nvenc` | 用 h264_nvenc 硬件编码（必带） |
| `--nvdec` | 用 h264_cuvid 硬件解码（必带） |
| `--workers N` | 并行 worker 数，默认 1 |
| `--no-motion-blur` | 跳过 7 子帧 motion blur，大幅提速 |
| `--no-cursor` | 不渲染鼠标 |
| `--blur-subframes N` | motion blur 子帧数，默认 7 |
| `--software-encoder` | 回退到 libx264 |
| `--chunk START:END` | 内部用，master 给 worker 传的 |

## 坑和注意事项

- **Windows 文件名消毒**由 `sanitize_filename()` 处理（移除 `<>:"/\|?*`）。如果
  项目名里带 `:`（时间戳常见），auto 输出路径会被清洗；要精确名字就显式 `-o`。

- **cuvid 必须限制 `-threads 4` 或更少**。超过就会触发 NVDEC 32 decode-surface
  上限，然后**静默回退到软件解码**（nvdec 利用率变 0）。这个坑踩过一次。

- **第一个 worker 通常比其他稍慢**，因为 chunk 起点的 decoder seek 有 warmup。

- **chunk 边界的 cursor spring 状态会重置**。第一帧用 raw 鼠标位置初始化，
  后续帧再 spring 跟随。分段处理通常看不出来。

- **不要误删 `.venv/`**，里面装了 opencv-python + numpy + Pillow，
  `requirements.txt` 能恢复。

## .gitignore 已经处理

- `*.mp4`, `*.mov` — 渲染输出
- `*.screenstudio/` — 源录像（体积大、私人内容）
- `.venv/`, `__pycache__/` — 常规
