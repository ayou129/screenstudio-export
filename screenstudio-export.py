#!/usr/bin/env python3
"""
screenstudio-export — Export Screen Studio projects to MP4 without a subscription.

Supported effects:
  - Zoom animations (manual, follow-mouse, follow-click-groups)
  - Speed changes (timeScale per slice)
  - Spring physics animations (viewport + cursor)
  - Cursor rendering with hotspot alignment
  - Motion blur on viewport transitions

Usage:
  python3 screenstudio-export.py <project.screenstudio> [options]

Options:
  -o, --output FILE       Output file path (default: <project-name>.mp4)
  --fps N                 Output frame rate (default: 60)
  --width N               Output width (default: source width from project)
  --height N              Output height (default: source height from project)
  --deadzone N            Follow-mode deadzone in pixels (default: 160)
  --blur-subframes N      Motion blur sub-frames, 1=off (default: 7)
  --bitrate STR           Video bitrate (default: 12M)
  --no-cursor             Hide cursor overlay
  --no-motion-blur        Disable motion blur
  --software-encoder      Use libx264 instead of hardware encoder
"""

import argparse
import bisect
import json
import math
import os
import platform
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


# Windows-invalid filename characters
_WIN_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    s = _WIN_INVALID.sub("_", name).strip().rstrip(".")
    return s or "output"


def load_json(path):
    with open(path, "r") as f:
        return json.load(f)


def ffprobe_dimensions(video_path):
    """Return (width, height) of the first video stream, or (None, None) on error."""
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json", str(video_path)
        ], stderr=subprocess.STDOUT, text=True)
        data = json.loads(out)
        stream = (data.get("streams") or [{}])[0]
        w = stream.get("width"); h = stream.get("height")
        if w and h:
            return int(w), int(h)
    except Exception:
        pass
    return None, None


def _rgba_to_yuv420_cursor(rgba_np):
    """Convert an RGBA cursor (H,W,4 uint8) into separate Y/U/V/alpha planes
    matching 4:2:0 chroma subsampling. Dimensions are padded to even.
    Returns dict with keys: y, u, v, alpha, alpha_uv, h, w."""
    h, w = rgba_np.shape[:2]
    # Pad to even dims for clean 4:2:0
    if h % 2 or w % 2:
        nh = h + (h % 2)
        nw = w + (w % 2)
        padded = np.zeros((nh, nw, 4), dtype=np.uint8)
        padded[:h, :w] = rgba_np
        rgba_np = padded
        h, w = nh, nw

    rgb = rgba_np[..., :3].astype(np.float32)
    alpha = (rgba_np[..., 3].astype(np.float32) * (1.0 / 255.0))

    # BT.709 full-range RGB → YUV (Y luma, UV chroma centered at 128)
    r = rgb[..., 0]; g = rgb[..., 1]; b = rgb[..., 2]
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    u = -0.1146 * r - 0.3854 * g + 0.5 * b + 128.0
    v = 0.5 * r - 0.4542 * g - 0.0458 * b + 128.0
    y_u8 = np.clip(y, 0, 255).astype(np.uint8)
    u_u8 = np.clip(u, 0, 255).astype(np.uint8)
    v_u8 = np.clip(v, 0, 255).astype(np.uint8)

    # Downsample U, V, alpha for 4:2:0
    uh, uw = h // 2, w // 2
    u_half = cv2.resize(u_u8, (uw, uh), interpolation=cv2.INTER_AREA)
    v_half = cv2.resize(v_u8, (uw, uh), interpolation=cv2.INTER_AREA)
    alpha_half = cv2.resize(alpha, (uw, uh), interpolation=cv2.INTER_AREA)

    return {
        "y": y_u8, "u": u_half, "v": v_half,
        "alpha": alpha, "alpha_uv": alpha_half,
        "h": h, "w": w,
    }


def parse_args():
    p = argparse.ArgumentParser(
        prog="screenstudio-export",
        description="Export Screen Studio projects to MP4",
    )
    p.add_argument("project", type=Path, help="Path to .screenstudio project directory")
    p.add_argument("-o", "--output", type=Path, default=None, help="Output MP4 path")
    p.add_argument("--fps", type=int, default=60, help="Output FPS (default: 60)")
    p.add_argument("--width", type=int, default=None,
                   help="Output width (default: source width from project bounds)")
    p.add_argument("--height", type=int, default=None,
                   help="Output height (default: source height from project bounds)")
    p.add_argument("--deadzone", type=int, default=160, help="Follow-mode deadzone px (default: 160)")
    p.add_argument("--blur-subframes", type=int, default=7, help="Motion blur sub-frames (default: 7)")
    p.add_argument("--bitrate", type=str, default="12M", help="Video bitrate (default: 12M)")
    p.add_argument("--no-cursor", action="store_true", help="Hide cursor")
    p.add_argument("--no-motion-blur", action="store_true", help="Disable motion blur")
    p.add_argument("--software-encoder", action="store_true", help="Use libx264 instead of HW encoder")
    p.add_argument("--nvenc", action="store_true", help="Use NVIDIA NVENC (h264_nvenc) hardware encoder")
    p.add_argument("--nvdec", action="store_true", help="Use NVIDIA NVDEC (CUDA) for input video decoding")
    p.add_argument("--workers", type=int, default=1,
                   help="Parallel worker processes (default: 1). Splits frame range and concatenates.")
    p.add_argument("--chunk", type=str, default=None, help=argparse.SUPPRESS)  # internal: START:END
    return p.parse_args()


# ─── Video frame reader ─────────────────────────────────────────────────────

class VideoFrameReader:
    """Decodes source video to numpy YUV420p (I420) planar frames.

    Output per frame is a tuple (Y, U, V) where:
        Y: (H, W)     uint8
        U: (H/2, W/2) uint8
        V: (H/2, W/2) uint8
    No RGB/BGR conversion happens — the frames flow as YUV end-to-end to the
    NVENC encoder which natively consumes 4:2:0. Optional NVDEC (cuvid) hwaccel.
    """

    def __init__(self, video_path, width, height, use_nvdec=False, decode_threads=2):
        assert width % 2 == 0 and height % 2 == 0, "width/height must be even for 4:2:0"
        self.width = width
        self.height = height
        self.uv_w = width // 2
        self.uv_h = height // 2
        self.y_bytes = width * height
        self.uv_bytes = self.uv_w * self.uv_h
        self.frame_size = self.y_bytes + 2 * self.uv_bytes  # 1.5 * W * H
        self.video_path = str(video_path)
        self.use_nvdec = use_nvdec
        self.decode_threads = decode_threads
        self.process = None
        self.current_time_ms = 0.0
        self._last_frame = None
        self._start_decoder(0)

    def _start_decoder(self, seek_ms):
        if self.process:
            try:
                self.process.stdout.close()
                self.process.kill()
                self.process.wait()
            except Exception:
                pass
        seek_s = max(0, seek_ms / 1000.0)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if self.use_nvdec:
            # cuvid decodes natively to NV12 on GPU; ffmpeg will downscale & convert
            # to yuv420p on the way out of pipe. Limit threads (32-surface NVDEC limit).
            cmd += ["-threads", str(self.decode_threads),
                    "-hwaccel", "cuda",
                    "-c:v", "h264_cuvid"]
        else:
            cmd += ["-threads", str(self.decode_threads)]
        cmd += [
            "-ss", f"{seek_s:.3f}",
            "-i", self.video_path,
            "-f", "rawvideo", "-pix_fmt", "yuv420p",
            "-s", f"{self.width}x{self.height}",
            "-r", "60", "-an", "pipe:1",
        ]
        self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=self.frame_size * 4)
        self.current_time_ms = seek_ms
        self._last_frame = None

    def _read_one(self):
        # One I420 frame = Y (W*H) + U (W/2*H/2) + V (W/2*H/2) contiguous
        data = self.process.stdout.read(self.frame_size)
        if not data or len(data) < self.frame_size:
            return None
        buf = np.frombuffer(data, dtype=np.uint8)
        y = buf[:self.y_bytes].reshape(self.height, self.width)
        u = buf[self.y_bytes:self.y_bytes + self.uv_bytes].reshape(self.uv_h, self.uv_w)
        v = buf[self.y_bytes + self.uv_bytes:].reshape(self.uv_h, self.uv_w)
        return (y, u, v)

    def read_frame_at(self, target_ms):
        if target_ms < self.current_time_ms - 100 or target_ms > self.current_time_ms + 10000:
            self._start_decoder(target_ms)
        interval = 1000.0 / 60.0
        while self.current_time_ms + interval < target_ms:
            frame = self._read_one()
            if frame is None:
                break
            self.current_time_ms += interval
            self._last_frame = frame
        frame = self._read_one()
        if frame is not None:
            self.current_time_ms += interval
            self._last_frame = frame
            return frame
        if self._last_frame is not None:
            return self._last_frame
        return (np.zeros((self.height, self.width), dtype=np.uint8),
                np.full((self.uv_h, self.uv_w), 128, dtype=np.uint8),
                np.full((self.uv_h, self.uv_w), 128, dtype=np.uint8))

    def close(self):
        if self.process:
            try:
                self.process.stdout.close()
                self.process.kill()
                self.process.wait()
            except Exception:
                pass


# ─── Project loader ──────────────────────────────────────────────────────────

class ScreenStudioProject:
    def __init__(self, project_dir, args):
        self.project_dir = Path(project_dir)
        self.recording_dir = self.project_dir / "recording"
        self.args = args

        # Validate
        if not self.project_dir.exists():
            sys.exit(f"Error: project not found: {self.project_dir}")
        if not (self.project_dir / "project.json").exists():
            sys.exit(f"Error: project.json not found in {self.project_dir}")
        if not self.recording_dir.exists():
            sys.exit(f"Error: recording directory not found in {self.project_dir}")

        # Load data
        project_data = load_json(self.project_dir / "project.json")
        self.project = project_data["json"]
        self.metadata = load_json(self.recording_dir / "metadata.json")
        self.config = self.project["config"]

        # Scene & effects
        self.scene = self.project["scenes"][0]
        self.zoom_ranges = self.scene.get("zoomRanges", [])
        self.slices = self.scene.get("slices", [])

        self.motion_blur_amount = self.config.get("motionBlurAmount", 1)

        # Source resolution from first display session
        self._load_sessions()
        # Resolve output resolution defaults: if --width/--height not given,
        # fall back to source bounds (snapped to even for 4:2:0).
        if self.args.width is None:
            self.args.width = self.source_width & ~1
        if self.args.height is None:
            self.args.height = self.source_height & ~1
        self._load_mouse_data()
        self._load_cursors()
        self._build_timeline()

    def _load_sessions(self):
        """Load session info and find video files."""
        display_recorders = [
            r for r in self.metadata["recorders"] if r["type"] == "display"
        ]
        if not display_recorders:
            sys.exit("Error: no display recorder found in metadata")

        self.display_recorder = display_recorders[0]
        self.sessions = self.display_recorder["sessions"]
        self.num_sessions = len(self.sessions)

        # Source resolution = bounds (logical display coords). The actual video
        # file may be HiDPI (e.g. 4096x2304 while bounds say 2560x1440). Using
        # bounds means the decoder asks ffmpeg to downscale — cheaper compose
        # per frame at modest quality cost (final output is 1080p anyway, and
        # the text-quality win comes from NVENC CQ mode + LANCZOS resize).
        bounds = self.sessions[0]["bounds"]
        self.source_width = bounds["width"]
        self.source_height = bounds["height"]
        self.coord_scale_x = 1.0
        self.coord_scale_y = 1.0

        # Session timing from metadata top-level sessions
        meta_sessions = self.metadata["sessions"]
        self.session_infos = []
        cumulative_source_ms = 0.0
        for i, ms in enumerate(meta_sessions):
            info = {
                "index": i,
                "processTimeStartMs": ms["processTimeStartMs"],
                "durationMs": ms["durationMs"],
                "sourceStartMs": cumulative_source_ms,
            }
            cumulative_source_ms += ms["durationMs"]
            self.session_infos.append(info)
        self.total_source_duration = cumulative_source_ms

        # Video file paths
        self.video_paths = []
        for s in self.sessions:
            vpath = self.recording_dir / s["outputFilename"]
            if not vpath.exists():
                sys.exit(f"Error: video file not found: {vpath}")
            self.video_paths.append(vpath)

        print(f"Project: {self.project.get('name', 'Untitled')}")
        print(f"Source: {self.source_width}x{self.source_height}, "
              f"{self.num_sessions} session(s), {self.total_source_duration/1000:.1f}s total")

    def _load_mouse_data(self):
        """Load mouse movements and clicks from all sessions."""
        input_recorders = [r for r in self.metadata["recorders"] if r["type"] == "input"]
        self.mouse_moves = []
        self.mouse_clicks = []

        if not input_recorders:
            return

        input_rec = input_recorders[0]
        for i, sess in enumerate(input_rec.get("sessions", [])):
            si = self.session_infos[i] if i < len(self.session_infos) else None
            if not si:
                continue

            process_start = si["processTimeStartMs"]
            source_offset = si["sourceStartMs"]

            sx = self.coord_scale_x
            sy = self.coord_scale_y

            # Mouse moves
            moves_file = sess.get("mouseMovesFilename")
            if moves_file and (self.recording_dir / moves_file).exists():
                data = load_json(self.recording_dir / moves_file)
                for evt in data:
                    evt["sourceTimeMs"] = evt["processTimeMs"] - process_start + source_offset
                    evt["x"] = evt["x"] * sx
                    evt["y"] = evt["y"] * sy
                    self.mouse_moves.append(evt)

            # Mouse clicks
            clicks_file = sess.get("mouseClicksFilename")
            if clicks_file and (self.recording_dir / clicks_file).exists():
                data = load_json(self.recording_dir / clicks_file)
                for evt in data:
                    evt["sourceTimeMs"] = evt["processTimeMs"] - process_start + source_offset
                    evt["x"] = evt["x"] * sx
                    evt["y"] = evt["y"] * sy
                    if evt["type"] == "mouseDown":
                        self.mouse_clicks.append(evt)

        self.mouse_moves.sort(key=lambda e: e["sourceTimeMs"])
        self.mouse_clicks.sort(key=lambda e: e["sourceTimeMs"])
        self.mouse_move_times = [e["sourceTimeMs"] for e in self.mouse_moves]
        self.mouse_click_times = [e["sourceTimeMs"] for e in self.mouse_clicks]

        print(f"Loaded {len(self.mouse_moves)} mouse moves, {len(self.mouse_clicks)} clicks")

    def _load_cursors(self):
        """Load cursor images and hotspots."""
        self.cursor_images = {}
        self.cursor_hotspots = {}

        if self.args.no_cursor:
            return

        cursors_path = self.recording_dir / "cursors.json"
        if not cursors_path.exists():
            return

        cursors_meta = load_json(cursors_path)
        size_mult = self.config.get("cursorSize", 1.5)

        # Scale cursor for output resolution
        output_scale = self.args.width / self.source_width
        effective_scale = size_mult * output_scale

        for cm in cursors_meta:
            cid = cm["id"]
            img_path = self.recording_dir / "cursors" / f"{cid}.png"
            if not img_path.exists():
                continue
            img = Image.open(img_path).convert("RGBA")
            new_w = max(1, int(cm["standardSize"]["width"] * effective_scale))
            new_h = max(1, int(cm["standardSize"]["height"] * effective_scale))
            img = img.resize((new_w, new_h), Image.LANCZOS)
            rgba = np.array(img, dtype=np.uint8)
            # Precompute YUV420-compatible cursor planes + alpha masks
            self.cursor_images[cid] = _rgba_to_yuv420_cursor(rgba)
            self.cursor_hotspots[cid] = (
                cm["hotSpot"]["x"] * effective_scale,
                cm["hotSpot"]["y"] * effective_scale,
            )

    def _build_timeline(self):
        """Build output timeline from slices."""
        self.slice_timeline = []
        t = 0.0
        for s in self.slices:
            src_dur = s["sourceEndMs"] - s["sourceStartMs"]
            out_dur = src_dur * s["timeScale"]
            self.slice_timeline.append({
                **s,
                "outputStartMs": t,
                "outputEndMs": t + out_dur,
            })
            t += out_dur
        self.total_output_ms = t
        self.total_output_frames = int(t / 1000.0 * self.args.fps) + 1
        self.slice_output_starts = [s["outputStartMs"] for s in self.slice_timeline]
        print(f"Output: {self.args.width}x{self.args.height} @ {self.args.fps}fps, "
              f"{self.total_output_ms/1000:.1f}s, {self.total_output_frames} frames")

    # ─── Time mapping ────────────────────────────────────────────────────────

    def output_to_source_time(self, t_out_ms):
        idx = bisect.bisect_right(self.slice_output_starts, t_out_ms) - 1
        idx = max(0, idx)
        s = self.slice_timeline[idx]
        t_out_ms = max(s["outputStartMs"], min(t_out_ms, s["outputEndMs"]))
        elapsed = t_out_ms - s["outputStartMs"]
        return s["sourceStartMs"] + (elapsed / s["timeScale"] if s["timeScale"] > 0 else 0)

    def source_time_to_session(self, source_ms):
        for i in range(self.num_sessions - 1, -1, -1):
            if source_ms >= self.session_infos[i]["sourceStartMs"]:
                return i, source_ms - self.session_infos[i]["sourceStartMs"]
        return 0, source_ms

    # ─── Mouse interpolation ─────────────────────────────────────────────────

    def get_mouse_pos(self, source_ms):
        if not self.mouse_moves:
            return self.source_width / 2, self.source_height / 2, "arrow"
        idx = bisect.bisect_right(self.mouse_move_times, source_ms) - 1
        if idx < 0:
            m = self.mouse_moves[0]
            return m["x"], m["y"], m.get("cursorId", "arrow")
        if idx >= len(self.mouse_moves) - 1:
            m = self.mouse_moves[-1]
            return m["x"], m["y"], m.get("cursorId", "arrow")
        m0, m1 = self.mouse_moves[idx], self.mouse_moves[idx + 1]
        dt = m1["sourceTimeMs"] - m0["sourceTimeMs"]
        t = max(0, min(1, (source_ms - m0["sourceTimeMs"]) / dt)) if dt > 0 else 0
        return (
            m0["x"] + (m1["x"] - m0["x"]) * t,
            m0["y"] + (m1["y"] - m0["y"]) * t,
            m0.get("cursorId", "arrow"),
        )

    def get_last_click_pos(self, source_ms):
        idx = bisect.bisect_right(self.mouse_click_times, source_ms) - 1
        if idx < 0:
            return None
        return self.mouse_clicks[idx]["x"], self.mouse_clicks[idx]["y"]

    # ─── Zoom target ─────────────────────────────────────────────────────────

    def get_zoom_target_viewport(self, source_ms):
        SW, SH = self.source_width, self.source_height
        deadzone = self.args.deadzone

        for zr in self.zoom_ranges:
            if zr.get("isDisabled", False):
                continue
            if not (zr["startTime"] <= source_ms <= zr["endTime"]):
                continue

            zoom = zr["zoom"]
            vp_w = SW / zoom
            vp_h = SH / zoom
            ztype = zr["type"]

            if ztype == "manual":
                tx = zr["manualTargetPoint"]["x"]
                ty = zr["manualTargetPoint"]["y"]
                cx = tx * (SW - vp_w) + vp_w / 2
                cy = ty * (SH - vp_h) + vp_h / 2

            elif ztype == "follow-mouse":
                mx, my, _ = self.get_mouse_pos(source_ms)
                dx = mx - self._follow_target[0]
                dy = my - self._follow_target[1]
                if math.sqrt(dx * dx + dy * dy) > deadzone:
                    self._follow_target = [mx, my]
                cx, cy = self._follow_target

            elif ztype == "follow-click-groups":
                cp = self.get_last_click_pos(source_ms)
                if cp:
                    dx = cp[0] - self._follow_target[0]
                    dy = cp[1] - self._follow_target[1]
                    if math.sqrt(dx * dx + dy * dy) > deadzone:
                        self._follow_target = [cp[0], cp[1]]
                cx, cy = self._follow_target

            else:
                cx, cy = SW / 2, SH / 2

            crop_x = max(0, min(cx - vp_w / 2, SW - vp_w))
            crop_y = max(0, min(cy - vp_h / 2, SH - vp_h))
            return crop_x, crop_y, vp_w, vp_h

        return 0.0, 0.0, float(SW), float(SH)

    # ─── Spring simulation ───────────────────────────────────────────────────

    def simulate_springs(self):
        print("Pre-simulating spring physics...")
        sp = self.config.get("screenMovementSpring", {"mass": 2.25, "stiffness": 200, "damping": 40})
        mass, stiff, damp = sp["mass"], sp["stiffness"], sp["damping"]
        dt = 1.0 / 2000.0  # 0.5ms steps

        total_steps = int(self.total_output_ms * 2) + 1
        total_ms = int(self.total_output_ms) + 1

        SW, SH = float(self.source_width), float(self.source_height)
        self.spring_vp = [(0.0, 0.0, SW, SH)] * total_ms
        self.spring_total_ms = total_ms
        self._follow_target = [self.source_width / 2, self.source_height / 2]

        px, py, pw, ph = 0.0, 0.0, SW, SH
        vx, vy, vw, vh = 0.0, 0.0, 0.0, 0.0

        for step in range(total_steps):
            out_ms = step * 0.5
            src_ms = self.output_to_source_time(out_ms)
            tx, ty, tw, th = self.get_zoom_target_viewport(src_ms)

            ax = (-stiff * (px - tx) - damp * vx) / mass
            ay = (-stiff * (py - ty) - damp * vy) / mass
            aw = (-stiff * (pw - tw) - damp * vw) / mass
            ah = (-stiff * (ph - th) - damp * vh) / mass

            vx += ax * dt; vy += ay * dt; vw += aw * dt; vh += ah * dt
            px += vx * dt; py += vy * dt; pw += vw * dt; ph += vh * dt

            ms_idx = int(out_ms)
            if ms_idx < total_ms:
                cw = max(100.0, min(pw, SW))
                ch = max(100.0, min(ph, SH))
                cx = max(0.0, min(px, SW - cw))
                cy = max(0.0, min(py, SH - ch))
                self.spring_vp[ms_idx] = (cx, cy, cw, ch)

        print("Spring simulation complete.")

    def get_viewport(self, t_out_ms):
        idx = max(0, min(int(t_out_ms), self.spring_total_ms - 1))
        return self.spring_vp[idx]

    def viewport_velocity(self, t_out_ms):
        idx = int(t_out_ms)
        if idx <= 0 or idx >= self.spring_total_ms - 1:
            return 0.0
        prev, curr = self.spring_vp[idx - 1], self.spring_vp[idx]
        frame_ms = 1000.0 / self.args.fps
        dx = (curr[0] - prev[0]) * frame_ms
        dy = (curr[1] - prev[1]) * frame_ms
        dw = (curr[2] - prev[2]) * frame_ms
        return math.sqrt(dx * dx + dy * dy) + abs(dw) * 2


# ─── Renderer ────────────────────────────────────────────────────────────────

class Renderer:
    def __init__(self, proj: ScreenStudioProject):
        self.proj = proj
        self.args = proj.args
        # Output dimensions snapped to even (4:2:0 requires even)
        self.out_w = self.args.width & ~1
        self.out_h = self.args.height & ~1
        self.out_w_h = self.out_w // 2
        self.out_h_h = self.out_h // 2

        # Limit cv2 internal threads so 4 workers don't oversubscribe a 32-thread CPU
        try:
            cv2.setNumThreads(max(1, int(os.environ.get("OPENCV_NUM_THREADS", "2"))))
        except Exception:
            pass

        # Cursor spring state
        ms_cfg = proj.config.get("mouseMovementSpring", {"mass": 3, "stiffness": 470, "damping": 70})
        self.ms_mass = ms_cfg["mass"]
        self.ms_stiff = ms_cfg["stiffness"]
        self.ms_damp = ms_cfg["damping"]
        self._cur = {"px": 0, "py": 0, "vx": 0, "vy": 0, "t": 0, "init": False}

    def smooth_cursor(self, t_out_ms, raw_x, raw_y):
        c = self._cur
        if not c["init"]:
            c["px"], c["py"] = raw_x, raw_y
            c["init"] = True
            c["t"] = t_out_ms
            return raw_x, raw_y
        dt_ms = t_out_ms - c["t"]
        if dt_ms <= 0:
            return c["px"], c["py"]
        steps = min(int(dt_ms), 200)
        dt_s = (dt_ms / steps) / 1000.0 if steps > 0 else 0.001
        px, py, vx, vy = c["px"], c["py"], c["vx"], c["vy"]
        for _ in range(steps):
            ax = (-self.ms_stiff * (px - raw_x) - self.ms_damp * vx) / self.ms_mass
            ay = (-self.ms_stiff * (py - raw_y) - self.ms_damp * vy) / self.ms_mass
            vx += ax * dt_s; vy += ay * dt_s
            px += vx * dt_s; py += vy * dt_s
        c.update(px=px, py=py, vx=vx, vy=vy, t=t_out_ms)
        return px, py

    def render_viewport_yuv(self, src, vp):
        """Crop a viewport region out of a source YUV420p frame and resize to output.
        src: tuple(Y, U, V) — planes at source dimensions.
        Returns (Y, U, V) at output dimensions.

        Sub-pixel precision via warpAffine: spring physics produces fractional
        viewport coords that change by <1 px per frame. Integer-snapping them
        quantizes motion into 1–2 px jumps that look like judder when upscaled
        to 1080p, especially during zoom-in. warpAffine samples from source with
        a fractional affine transform, so the animation is smooth.
        """
        y_src, u_src, v_src = src
        SW, SH = self.proj.source_width, self.proj.source_height
        cx, cy, cw, ch = vp
        cx = max(0.0, min(float(cx), SW - 2.0))
        cy = max(0.0, min(float(cy), SH - 2.0))
        cw = max(2.0, min(float(cw), SW - cx))
        ch = max(2.0, min(float(ch), SH - cy))

        sx = cw / self.out_w
        sy = ch / self.out_h

        # warpAffine doesn't support INTER_AREA. For downscale use CUBIC
        # (decent + fast); for upscale use LANCZOS4 (sharp text).
        if sx >= 1.0 and sy >= 1.0:
            interp = cv2.INTER_CUBIC
        else:
            interp = cv2.INTER_LANCZOS4
        flags = interp | cv2.WARP_INVERSE_MAP

        M_y = np.array([[sx, 0.0, cx], [0.0, sy, cy]], dtype=np.float32)
        y_out = cv2.warpAffine(y_src, M_y, (self.out_w, self.out_h),
                               flags=flags, borderMode=cv2.BORDER_REPLICATE)

        # U/V planes are half-res in both source and output, so scale factor is
        # unchanged; only translation halves.
        M_uv = np.array([[sx, 0.0, cx * 0.5], [0.0, sy, cy * 0.5]], dtype=np.float32)
        u_out = cv2.warpAffine(u_src, M_uv, (self.out_w_h, self.out_h_h),
                               flags=flags, borderMode=cv2.BORDER_REPLICATE)
        v_out = cv2.warpAffine(v_src, M_uv, (self.out_w_h, self.out_h_h),
                               flags=flags, borderMode=cv2.BORDER_REPLICATE)
        return (y_out, u_out, v_out)

    def blit_cursor_yuv(self, y, u, v, cursor, px, py):
        """Alpha-composite a precomputed YUV cursor onto a (Y, U, V) output frame
        in-place. `cursor` is a dict produced by _rgba_to_yuv420_cursor."""
        # Snap top-left to even so UV blit aligns with 4:2:0
        px = int(px) & ~1
        py = int(py) & ~1
        cw = cursor["w"]; ch = cursor["h"]

        x0 = max(0, px); y0 = max(0, py)
        x1 = min(self.out_w, px + cw); y1 = min(self.out_h, py + ch)
        # Snap clipped extents back to even
        x0 &= ~1; y0 &= ~1
        x1 &= ~1; y1 &= ~1
        if x0 >= x1 or y0 >= y1:
            return
        sx0 = x0 - px; sy0 = y0 - py
        rw = x1 - x0; rh = y1 - y0

        # ---- Y plane ----
        cy = cursor["y"][sy0:sy0 + rh, sx0:sx0 + rw]
        ca = cursor["alpha"][sy0:sy0 + rh, sx0:sx0 + rw]
        region_y = y[y0:y1, x0:x1]
        blended = region_y.astype(np.float32) * (1.0 - ca) + cy.astype(np.float32) * ca
        y[y0:y1, x0:x1] = blended.astype(np.uint8)

        # ---- U, V planes (half resolution) ----
        hx0 = x0 // 2; hy0 = y0 // 2
        hx1 = x1 // 2; hy1 = y1 // 2
        hsx0 = sx0 // 2; hsy0 = sy0 // 2
        hrw = hx1 - hx0; hrh = hy1 - hy0
        if hrw <= 0 or hrh <= 0:
            return
        cu = cursor["u"][hsy0:hsy0 + hrh, hsx0:hsx0 + hrw]
        cv_ = cursor["v"][hsy0:hsy0 + hrh, hsx0:hsx0 + hrw]
        ca_uv = cursor["alpha_uv"][hsy0:hsy0 + hrh, hsx0:hsx0 + hrw]
        ru = u[hy0:hy1, hx0:hx1].astype(np.float32)
        rv = v[hy0:hy1, hx0:hx1].astype(np.float32)
        u[hy0:hy1, hx0:hx1] = (ru * (1.0 - ca_uv) + cu.astype(np.float32) * ca_uv).astype(np.uint8)
        v[hy0:hy1, hx0:hx1] = (rv * (1.0 - ca_uv) + cv_.astype(np.float32) * ca_uv).astype(np.uint8)

    def _pick_encoder_args(self):
        args = self.args
        if args.software_encoder:
            return ["-c:v", "libx264", "-crf", "16", "-preset", "medium"]
        if args.nvenc:
            # High-quality CQ mode: constant-quality VBR, capped at 60 Mbps.
            # -cq 18 is visually near-lossless for 1080p; survives platform re-encodes.
            return [
                "-c:v", "h264_nvenc",
                "-preset", "p6",        # second-highest quality preset
                "-tune", "hq",
                "-profile:v", "high",
                "-rc", "vbr",
                "-cq", "18",
                "-b:v", "0",
                "-maxrate", "60M",
                "-bufsize", "120M",
                "-spatial-aq", "1",
                "-temporal-aq", "1",
                "-rc-lookahead", "20",
            ]
        if platform.system() == "Darwin":
            return ["-c:v", "h264_videotoolbox", "-b:v", args.bitrate, "-profile:v", "high"]
        return ["-c:v", "libx264", "-crf", "16", "-preset", "medium"]

    def run(self, chunk_start=0, chunk_end=None):
        proj = self.proj
        args = self.args
        fps = args.fps
        frame_ms = 1000.0 / fps
        blur_n = args.blur_subframes if not args.no_motion_blur else 1

        total_all = proj.total_output_frames
        if chunk_end is None or chunk_end > total_all:
            chunk_end = total_all
        chunk_start = max(0, chunk_start)
        chunk_total = max(0, chunk_end - chunk_start)
        is_chunk = (chunk_start > 0) or (chunk_end < total_all)

        # Source dims snapped to even (4:2:0)
        src_w = proj.source_width & ~1
        src_h = proj.source_height & ~1

        # Open video readers (YUV420p output, optional NVDEC)
        readers = [
            VideoFrameReader(vp, src_w, src_h, use_nvdec=args.nvdec, decode_threads=2)
            for vp in proj.video_paths
        ]

        # Output path (Windows-safe filename)
        output = args.output
        if output is None:
            name = sanitize_filename(proj.project.get("name", "output"))
            output = proj.project_dir.parent / f"{name}.mp4"

        codec_args = self._pick_encoder_args()
        # Encoder input is YUV420p (yuv420p = I420) — NO swscale on the encoder side.
        # Frame size for stdin buffer: Y + U + V = W * H * 1.5
        enc_frame_bytes = self.out_w * self.out_h * 3 // 2
        enc_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-f", "rawvideo", "-pix_fmt", "yuv420p",
            "-s", f"{self.out_w}x{self.out_h}",
            "-r", str(fps), "-i", "pipe:0",
            *codec_args,
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(output),
        ]
        encoder = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE,
                                   bufsize=enc_frame_bytes * 2)

        total = chunk_total
        label = f"[{chunk_start}:{chunk_end}] " if is_chunk else ""
        print(f"Rendering {label}{total} frames -> {output.name}")
        print(f"  Encoder: {codec_args[1]}   Decoder: {'NVDEC' if args.nvdec else 'CPU'}")
        if blur_n > 1:
            print(f"  Motion blur: {blur_n} sub-frames")

        # Precompute the frame plan (only the requested chunk)
        plan = []
        for fi in range(chunk_start, chunk_end):
            t_out = fi * frame_ms
            t_src = proj.output_to_source_time(t_out)
            si, vid_t = proj.source_time_to_session(t_src)
            plan.append((fi, t_out, t_src, si, vid_t))

        # Decoder thread feeds raw YUV frames through a bounded queue (no color conversion).
        raw_q: "queue.Queue" = queue.Queue(maxsize=8)
        stop_flag = threading.Event()

        def decoder_worker():
            try:
                for fi, _t_out, _t_src, si, vid_t in plan:
                    if stop_flag.is_set():
                        break
                    yuv = readers[si].read_frame_at(vid_t)
                    raw_q.put((fi, yuv))
            except Exception as e:
                raw_q.put(("__error__", e))
            finally:
                raw_q.put(None)

        dec_thread = threading.Thread(target=decoder_worker, daemon=True)
        dec_thread.start()

        # Motion-blur accumulators (one per plane)
        y_acc = np.zeros((self.out_h, self.out_w), dtype=np.float32)
        u_acc = np.zeros((self.out_h_h, self.out_w_h), dtype=np.float32)
        v_acc = np.zeros((self.out_h_h, self.out_w_h), dtype=np.float32)

        last_pct = -1
        frames_done = 0
        start_time = time.time()
        try:
            for fi, t_out, t_src, si, vid_t in plan:
                item = raw_q.get()
                if item is None:
                    break
                if isinstance(item[0], str) and item[0] == "__error__":
                    raise item[1]
                _q_fi, src_yuv = item
                vp = proj.get_viewport(t_out)
                vel = proj.viewport_velocity(t_out)

                if vel > 5.0 and blur_n > 1 and proj.motion_blur_amount > 0:
                    y_acc.fill(0); u_acc.fill(0); v_acc.fill(0)
                    for s in range(blur_n):
                        sub_t = t_out + (s - blur_n // 2) * (frame_ms / blur_n) * 0.5
                        sub_t = max(0.0, min(sub_t, proj.total_output_ms))
                        sub_vp = proj.get_viewport(sub_t)
                        sy, su, sv = self.render_viewport_yuv(src_yuv, sub_vp)
                        cv2.accumulate(sy, y_acc)
                        cv2.accumulate(su, u_acc)
                        cv2.accumulate(sv, v_acc)
                    inv = 1.0 / blur_n
                    y_out = cv2.convertScaleAbs(y_acc, alpha=inv)
                    u_out = cv2.convertScaleAbs(u_acc, alpha=inv)
                    v_out = cv2.convertScaleAbs(v_acc, alpha=inv)
                else:
                    y_out, u_out, v_out = self.render_viewport_yuv(src_yuv, vp)

                # Cursor overlay — YUV planes with precomputed chroma+alpha
                if not args.no_cursor and proj.cursor_images:
                    raw_mx, raw_my, cid = proj.get_mouse_pos(t_src)
                    smx, smy = self.smooth_cursor(t_out, raw_mx, raw_my)
                    cx, cy, cw, ch = vp
                    cursor = proj.cursor_images.get(cid)
                    if cursor is not None:
                        hx, hy = proj.cursor_hotspots[cid]
                        sx = (smx - cx) * (self.out_w / cw)
                        sy = (smy - cy) * (self.out_h / ch)
                        px, py = int(sx - hx), int(sy - hy)
                        self.blit_cursor_yuv(y_out, u_out, v_out, cursor, px, py)

                # Write planar I420: Y, then U, then V
                enc_in = encoder.stdin
                enc_in.write(y_out.tobytes())
                enc_in.write(u_out.tobytes())
                enc_in.write(v_out.tobytes())
                frames_done += 1

                if frames_done % 600 == 0 or frames_done == total:
                    pct = frames_done * 100 // total
                    elapsed = time.time() - start_time
                    rate = frames_done / elapsed if elapsed > 0 else 0
                    eta = (total - frames_done) / rate if rate > 0 else 0
                    print(f"  {pct:3d}% ({frames_done}/{total})  {rate:.1f} fps  ETA {eta:5.0f}s",
                          flush=True)
        except (BrokenPipeError, KeyboardInterrupt):
            stop_flag.set()
            raise
        finally:
            stop_flag.set()
            try:
                encoder.stdin.close()
            except Exception:
                pass
            encoder.wait()
            dec_thread.join(timeout=2.0)
            for r in readers:
                r.close()

        size_mb = output.stat().st_size / (1024 * 1024)
        elapsed = time.time() - start_time
        print(f"\nDone! {output}")
        print(f"  {size_mb:.1f} MB, {proj.total_output_ms/1000:.1f}s, "
              f"{self.out_w}x{self.out_h} @ {fps}fps  (rendered in {elapsed:.1f}s)")


# ─── Main ────────────────────────────────────────────────────────────────────

def _resolve_project_path(arg_path: Path) -> Path:
    if not str(arg_path).endswith(".screenstudio"):
        candidates = list(arg_path.glob("*.screenstudio")) if arg_path.is_dir() else []
        if candidates:
            return candidates[0]
        sys.exit(f"Error: {arg_path} is not a .screenstudio project")
    return arg_path


def run_multiprocess(args, proj_path: Path):
    """Master: split frame range into N chunks, spawn N worker subprocesses, concat."""
    # Load project briefly to get total frame count and default output path
    proj = ScreenStudioProject(proj_path, args)
    total = proj.total_output_frames
    proj_name = sanitize_filename(proj.project.get("name", "output"))

    # Resolve final output path
    final_out = args.output
    if final_out is None:
        final_out = proj_path.parent / f"{proj_name}.mp4"
    final_out = Path(final_out)

    n = max(1, args.workers)
    # Split into N roughly-equal chunks
    base = total // n
    rem = total % n
    chunks = []
    cur = 0
    for i in range(n):
        sz = base + (1 if i < rem else 0)
        if sz <= 0:
            continue
        chunks.append((i, cur, cur + sz))
        cur += sz

    temp_dir = final_out.parent / f".{final_out.stem}_parts"
    temp_dir.mkdir(exist_ok=True)

    print(f"\n{'='*60}")
    print(f"Multi-worker mode: {len(chunks)} workers, {total} total frames")
    for i, s, e in chunks:
        print(f"  W{i}: frames {s}..{e}  ({e-s} frames)")
    print(f"{'='*60}\n")

    # Spawn workers
    script = os.path.abspath(sys.argv[0])
    python = sys.executable
    procs = []
    temp_files = []
    for i, s, e in chunks:
        tpath = temp_dir / f"part_{i:03d}.mp4"
        temp_files.append(tpath)
        cmd = [
            python, "-u", script, str(proj_path),
            "-o", str(tpath),
            "--chunk", f"{s}:{e}",
            "--workers", "1",
            "--fps", str(args.fps),
            "--width", str(args.width),
            "--height", str(args.height),
            "--deadzone", str(args.deadzone),
            "--blur-subframes", str(args.blur_subframes),
            "--bitrate", str(args.bitrate),
        ]
        if args.no_cursor: cmd.append("--no-cursor")
        if args.no_motion_blur: cmd.append("--no-motion-blur")
        if args.software_encoder: cmd.append("--software-encoder")
        if args.nvenc: cmd.append("--nvenc")
        if args.nvdec: cmd.append("--nvdec")

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                bufsize=1, universal_newlines=True, env=env)
        procs.append((i, proc))

    # Stream worker output with prefix
    def pipe_reader(idx, proc):
        try:
            for line in proc.stdout:
                print(f"[W{idx}] {line.rstrip()}", flush=True)
        except Exception:
            pass

    reader_threads = []
    for idx, proc in procs:
        t = threading.Thread(target=pipe_reader, args=(idx, proc), daemon=True)
        t.start()
        reader_threads.append(t)

    start_time = time.time()
    failures = []
    for idx, proc in procs:
        rc = proc.wait()
        if rc != 0:
            failures.append((idx, rc))
    for t in reader_threads:
        t.join(timeout=2.0)
    elapsed = time.time() - start_time

    if failures:
        print(f"\nWorker failures: {failures}", flush=True)
        sys.exit(1)

    # Verify all parts exist
    missing = [str(p) for p in temp_files if not p.exists()]
    if missing:
        print(f"\nMissing parts: {missing}", flush=True)
        sys.exit(1)

    print(f"\nAll workers done in {elapsed:.1f}s. Concatenating {len(temp_files)} parts...", flush=True)

    list_file = temp_dir / "concat_list.txt"
    list_file.write_text("\n".join(f"file '{p.name}'" for p in temp_files), encoding="utf-8")

    # ffmpeg concat: run from inside temp_dir so relative filenames in the list resolve
    final_out_abs = final_out.resolve()
    concat_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-f", "concat", "-safe", "0", "-i", "concat_list.txt",
        "-c", "copy", "-movflags", "+faststart", str(final_out_abs),
    ]
    r = subprocess.run(concat_cmd, cwd=str(temp_dir))
    if r.returncode != 0:
        print("Stream copy concat failed, re-encoding via NVENC...", flush=True)
        concat_cmd2 = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-f", "concat", "-safe", "0", "-i", "concat_list.txt",
            "-c:v", "h264_nvenc", "-b:v", args.bitrate, "-preset", "p5",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(final_out_abs),
        ]
        subprocess.run(concat_cmd2, cwd=str(temp_dir), check=True)

    # Cleanup
    for p in temp_files:
        try: p.unlink()
        except Exception: pass
    try: list_file.unlink()
    except Exception: pass
    try: temp_dir.rmdir()
    except Exception: pass

    size_mb = final_out.stat().st_size / (1024 * 1024)
    total_elapsed = time.time() - start_time
    print(f"\nDone! {final_out}")
    print(f"  {size_mb:.1f} MB  ({total} frames in {total_elapsed:.1f}s, "
          f"effective {total/total_elapsed:.1f} fps)")


def main():
    args = parse_args()
    proj_path = _resolve_project_path(args.project)

    # Worker mode: render one chunk and exit
    if args.chunk:
        try:
            cs, ce = args.chunk.split(":")
            chunk_start, chunk_end = int(cs), int(ce)
        except ValueError:
            sys.exit(f"Error: invalid --chunk format: {args.chunk}")
        proj = ScreenStudioProject(proj_path, args)
        proj.simulate_springs()
        Renderer(proj).run(chunk_start=chunk_start, chunk_end=chunk_end)
        return

    # Master multi-worker mode
    if args.workers and args.workers > 1:
        run_multiprocess(args, proj_path)
        return

    # Single-process mode
    proj = ScreenStudioProject(proj_path, args)
    proj.simulate_springs()
    Renderer(proj).run()


if __name__ == "__main__":
    main()
