#!/usr/bin/env python3
"""Synthesize speech with edge-tts and play it into a BlackHole device."""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
import contextlib
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import edge_tts
import numpy as np
import sounddevice as sd


DEFAULT_TEXT = "你好，这是一段通过 BlackHole 虚拟麦克风播放的测试语音。"
DEFAULT_VOICE = "zh-CN-YunjianNeural"
EDGE_TTS_SAMPLE_RATE = 24_000
CHANNELS = 2
SAMPLE_RATE_STABLE_SECONDS = 2.0
INTERACTIVE_BLOCK_MS = 20
INTERACTIVE_MODES = ("console", "mix", "ptt")
TTS_CHUNK_MAX_CHARS = 140
TTS_DOWNLOAD_ATTEMPTS = 3


def split_tts_text(text: str, max_chars: int = TTS_CHUNK_MAX_CHARS) -> list[str]:
    if max_chars <= 0:
        raise ValueError("TTS 分段长度必须大于 0")
    units: list[str] = []
    for raw_line in text.splitlines() or [text]:
        line = re.sub(r"^\s*[*+-]\s+", "", raw_line).strip()
        if not line:
            continue
        for sentence in re.split(r"(?<=[。！？!?；;])\s*", line):
            sentence = sentence.strip()
            while len(sentence) > max_chars:
                units.append(sentence[:max_chars])
                sentence = sentence[max_chars:].strip()
            if sentence:
                units.append(sentence)

    chunks: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current} {unit}".strip()
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = unit
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def comparable_tts_text(text: str) -> str:
    return re.sub(r"[^\w]", "", text, flags=re.UNICODE).replace("_", "").casefold()


def find_device(name: str) -> int:
    matches = [
        (index, device)
        for index, device in enumerate(sd.query_devices())
        if name.casefold() in str(device["name"]).casefold()
        and int(device["max_output_channels"]) >= CHANNELS
    ]
    if not matches:
        raise RuntimeError(f"找不到支持双声道输出的音频设备：{name}")
    if len(matches) > 1:
        names = ", ".join(f"{index}: {device['name']}" for index, device in matches)
        raise RuntimeError(f"设备名匹配到多个结果，请使用更精确的名称：{names}")
    return matches[0][0]


def find_input_device(name: str) -> int:
    matches = [
        (index, device)
        for index, device in enumerate(sd.query_devices())
        if name.casefold() in str(device["name"]).casefold()
        and int(device["max_input_channels"]) >= 1
    ]
    if not matches:
        raise RuntimeError(f"找不到音频输入设备：{name}")
    if len(matches) > 1:
        names = ", ".join(f"{index}: {device['name']}" for index, device in matches)
        raise RuntimeError(f"麦克风名称匹配到多个结果，请使用更精确的名称：{names}")
    return matches[0][0]


def get_sample_rate(device_index: int) -> int:
    return round(float(sd.query_devices(device_index)["default_samplerate"]))


def wait_for_stable_sample_rate(device_index: int, timeout: float = 10.0) -> int:
    started_at = time.monotonic()
    sample_rate = get_sample_rate(device_index)
    stable_since = started_at
    while time.monotonic() - started_at < timeout:
        time.sleep(0.1)
        current = get_sample_rate(device_index)
        if current != sample_rate:
            print(f"检测到设备采样率变化：{sample_rate} Hz -> {current} Hz")
            sample_rate = current
            stable_since = time.monotonic()
        if time.monotonic() - stable_since >= SAMPLE_RATE_STABLE_SECONDS:
            return sample_rate
    raise RuntimeError("BlackHole 采样率在 10 秒内未稳定，请检查正在使用该设备的应用")


async def synthesize(text: str, voice: str, rate: str, volume: str, output: Path) -> None:
    await edge_tts.Communicate(text, voice, rate=rate, volume=volume).save(str(output))


def find_audiotoolbox_device(name: str, sample_rate: int) -> int:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg，请先安装：brew install ffmpeg")
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r={sample_rate}:cl=stereo",
            "-t",
            "0.01",
            "-list_devices",
            "true",
            "-f",
            "audiotoolbox",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    matches = []
    for line in result.stderr.splitlines():
        match = re.search(r"\[(\d+)]\s+(.+?),\s*\S+\s*$", line)
        if match and name.casefold() in match.group(2).strip().casefold():
            matches.append((int(match.group(1)), match.group(2).strip()))
    if len(matches) != 1:
        raise RuntimeError(f"FFmpeg AudioToolbox 无法唯一匹配设备：{name}")
    return matches[0][0]


def play(
    audio_file: Path,
    device_index: int,
    device_name: str,
    sample_rate: int,
    verify: bool,
) -> float | None:
    captured: list[np.ndarray] = []
    stop_capture = threading.Event()

    def capture() -> None:
        if not verify:
            return
        with sd.InputStream(
            samplerate=sample_rate,
            channels=CHANNELS,
            dtype="float32",
            device=device_index,
        ) as stream:
            while not stop_capture.is_set():
                data, _ = stream.read(sample_rate // 10)
                captured.append(data.copy())

    capture_thread = threading.Thread(target=capture, daemon=True)
    capture_thread.start()
    try:
        ffmpeg = shutil.which("ffmpeg")
        assert ffmpeg is not None
        audiotoolbox_index = find_audiotoolbox_device(device_name, sample_rate)
        subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-re",
                "-i",
                str(audio_file),
                "-af",
                f"aresample={sample_rate}:osf=flt",
                "-ar",
                str(sample_rate),
                "-ac",
                str(CHANNELS),
                "-c:a",
                "pcm_f32le",
                "-audio_device_index",
                str(audiotoolbox_index),
                "-f",
                "audiotoolbox",
                "-",
            ],
            check=True,
        )
    finally:
        stop_capture.set()
        capture_thread.join(timeout=2)

    if not verify or not captured:
        return None
    samples = np.concatenate(captured).astype(np.float64)
    rms = float(np.sqrt(np.mean(samples * samples)))
    return 20 * np.log10(max(rms, 1e-12))


async def stream_to_blackhole(
    text: str,
    voice: str,
    rate: str,
    volume: str,
    device_index: int,
    device_name: str,
    sample_rate: int,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg，请先安装：brew install ffmpeg")
    audiotoolbox_index = find_audiotoolbox_device(device_name, sample_rate)
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-v",
        "error",
        "-re",
        "-f",
        "mp3",
        "-i",
        "pipe:0",
        "-af",
        f"aresample={sample_rate}:osf=flt",
        "-ar",
        str(sample_rate),
        "-ac",
        str(CHANNELS),
        "-c:a",
        "pcm_f32le",
        "-audio_device_index",
        str(audiotoolbox_index),
        "-f",
        "audiotoolbox",
        "-",
        stdin=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None
    started_at = time.monotonic()
    first_audio_at: float | None = None
    audio_bytes = 0
    changed_sample_rate: int | None = None

    async def monitor_sample_rate() -> None:
        nonlocal changed_sample_rate
        while process.returncode is None:
            current = get_sample_rate(device_index)
            if current != sample_rate:
                changed_sample_rate = current
                process.terminate()
                return
            await asyncio.sleep(0.2)

    monitor_task = asyncio.create_task(monitor_sample_rate())
    try:
        communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume)
        async for chunk in communicate.stream():
            if chunk["type"] != "audio":
                continue
            if first_audio_at is None:
                first_audio_at = time.monotonic()
                print(f"首个音频块：{first_audio_at - started_at:.2f} 秒")
            data = chunk["data"]
            audio_bytes += len(data)
            process.stdin.write(data)
            await process.stdin.drain()
        print(f"TTS 音频流接收完成：{time.monotonic() - started_at:.2f} 秒")
    except BaseException:
        if process.returncode is None:
            process.terminate()
        raise
    finally:
        if not process.stdin.is_closing():
            process.stdin.close()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await process.stdin.wait_closed()
    return_code = await process.wait()
    monitor_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await monitor_task
    if changed_sample_rate is not None:
        raise RuntimeError(
            f"播放期间设备采样率发生变化：{sample_rate} Hz -> {changed_sample_rate} Hz；"
            "已停止推送以避免变速失真"
        )
    if return_code != 0:
        raise RuntimeError(f"FFmpeg 流式播放失败，退出码：{return_code}")
    if first_audio_at is None or audio_bytes == 0:
        raise RuntimeError("Edge TTS 没有返回音频数据")
    print(f"流式音频：{audio_bytes} 字节，总耗时：{time.monotonic() - started_at:.2f} 秒")


class PCMBuffer:
    """Thread-safe FIFO for decoded stereo float32 TTS frames."""

    def __init__(self) -> None:
        self._chunks: deque[np.ndarray] = deque()
        self._head_offset = 0
        self._active_utterances = 0
        self._lock = threading.Lock()

    def begin_utterance(self) -> None:
        with self._lock:
            self._active_utterances += 1

    def end_utterance(self) -> None:
        with self._lock:
            if self._active_utterances <= 0:
                raise RuntimeError("TTS 语句状态不匹配")
            self._active_utterances -= 1

    def is_active(self) -> bool:
        with self._lock:
            return self._active_utterances > 0 or bool(self._chunks)

    def append(self, frames: np.ndarray) -> None:
        if frames.ndim != 2 or frames.shape[1] != CHANNELS:
            raise ValueError("PCM 数据必须是双声道")
        if frames.size == 0:
            return
        with self._lock:
            self._chunks.append(np.ascontiguousarray(frames, dtype=np.float32))

    def read(self, frame_count: int) -> np.ndarray:
        output = np.zeros((frame_count, CHANNELS), dtype=np.float32)
        written = 0
        with self._lock:
            while written < frame_count and self._chunks:
                chunk = self._chunks[0]
                available = len(chunk) - self._head_offset
                take = min(frame_count - written, available)
                output[written : written + take] = chunk[
                    self._head_offset : self._head_offset + take
                ]
                written += take
                self._head_offset += take
                if self._head_offset == len(chunk):
                    self._chunks.popleft()
                    self._head_offset = 0
        return output


class TTSStreamWorker:
    def __init__(
        self,
        pcm_buffer: PCMBuffer,
        sample_rate: int,
        voice: str,
        rate: str,
        volume: str,
    ) -> None:
        self._pcm_buffer = pcm_buffer
        self._sample_rate = sample_rate
        self._voice = voice
        self._rate = rate
        self._volume = volume
        self._texts: queue.Queue[str | None] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="edge-tts-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, text: str) -> None:
        self._texts.put(text)

    def stop(self) -> None:
        self._stop.set()
        self._texts.put(None)
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise RuntimeError("TTS 后台线程未能及时停止")

    def _run(self) -> None:
        while not self._stop.is_set():
            text = self._texts.get()
            if text is None:
                return
            try:
                asyncio.run(self._synthesize(text))
            except Exception as exc:
                print(f"\nTTS 合成失败：{exc}", file=sys.stderr, flush=True)

    async def _download_segment(
        self,
        segment: str,
        segment_index: int,
        segment_count: int,
    ) -> bytes | None:
        expected = comparable_tts_text(segment)
        last_error: Exception | None = None
        for attempt in range(1, TTS_DOWNLOAD_ATTEMPTS + 1):
            if self._stop.is_set():
                return None
            try:
                media = bytearray()
                boundaries: list[str] = []
                communicate = edge_tts.Communicate(
                    segment,
                    self._voice,
                    rate=self._rate,
                    volume=self._volume,
                )
                async for chunk in communicate.stream():
                    if self._stop.is_set():
                        return None
                    if chunk["type"] == "audio":
                        media.extend(chunk["data"])
                    elif chunk["type"] == "SentenceBoundary":
                        boundaries.append(str(chunk.get("text", "")))
                if not media:
                    raise RuntimeError("Edge TTS 没有返回音频数据")
                spoken = comparable_tts_text("".join(boundaries))
                expected_tail = expected[-min(12, len(expected)) :]
                if spoken and expected_tail and expected_tail not in spoken:
                    raise RuntimeError(
                        f"Edge TTS 返回内容不完整，期望结尾：{expected_tail}"
                    )
                return bytes(media)
            except Exception as exc:
                last_error = exc
                if attempt < TTS_DOWNLOAD_ATTEMPTS:
                    print(
                        f"\nTTS 第 {segment_index}/{segment_count} 段获取失败，"
                        f"正在重试（{attempt}/{TTS_DOWNLOAD_ATTEMPTS}）：{exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    await asyncio.sleep(0.25 * attempt)
        raise RuntimeError(
            f"TTS 第 {segment_index}/{segment_count} 段连续 "
            f"{TTS_DOWNLOAD_ATTEMPTS} 次获取失败"
        ) from last_error

    async def _synthesize(self, text: str) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("找不到 ffmpeg，请先安装：brew install ffmpeg")
        segments = split_tts_text(text)
        if not segments:
            raise RuntimeError("没有可合成的文本")
        decoder = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-v",
            "error",
            "-f",
            "mp3",
            "-i",
            "pipe:0",
            "-af",
            f"aresample={self._sample_rate}:osf=flt",
            "-ar",
            str(self._sample_rate),
            "-ac",
            str(CHANNELS),
            "-c:a",
            "pcm_f32le",
            "-f",
            "f32le",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert decoder.stdin is not None
        assert decoder.stdout is not None
        utterance_started = False
        decoded_frames = 0

        async def read_pcm() -> None:
            nonlocal decoded_frames, utterance_started
            remainder = b""
            while data := await decoder.stdout.read(32 * 1024):
                raw = remainder + data
                usable = len(raw) - len(raw) % (CHANNELS * 4)
                if usable:
                    frames = np.frombuffer(raw[:usable], dtype="<f4").copy()
                    shaped_frames = frames.reshape(-1, CHANNELS)
                    if not utterance_started:
                        self._pcm_buffer.begin_utterance()
                        utterance_started = True
                    self._pcm_buffer.append(shaped_frames)
                    decoded_frames += len(shaped_frames)
                remainder = raw[usable:]
            if remainder:
                raise RuntimeError("FFmpeg 返回了不完整的 float32 PCM 帧")

        reader_task = asyncio.create_task(read_pcm())
        started_at = time.monotonic()
        first_audio_at: float | None = None
        audio_bytes = 0
        stopped = False
        producer_error: BaseException | None = None
        try:
            for segment_index, segment in enumerate(segments, start=1):
                media = await self._download_segment(
                    segment,
                    segment_index,
                    len(segments),
                )
                if media is None:
                    stopped = True
                    break
                if first_audio_at is None:
                    first_audio_at = time.monotonic()
                    print(
                        f"\nTTS 首个音频块：{first_audio_at - started_at:.2f} 秒",
                        flush=True,
                    )
                audio_bytes += len(media)
                decoder.stdin.write(media)
                await decoder.stdin.drain()
        except BaseException as exc:
            producer_error = exc
        finally:
            if not decoder.stdin.is_closing():
                decoder.stdin.close()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    await decoder.stdin.wait_closed()

        reader_error: BaseException | None = None
        try:
            await reader_task
        except BaseException as exc:
            reader_error = exc
        return_code = await decoder.wait()
        if utterance_started:
            self._pcm_buffer.end_utterance()
        if producer_error is not None:
            raise producer_error
        if reader_error is not None:
            raise reader_error
        if stopped:
            return
        if return_code != 0:
            stderr = await decoder.stderr.read() if decoder.stderr else b""
            message = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(message or f"FFmpeg 解码失败，退出码：{return_code}")
        if first_audio_at is None or audio_bytes == 0 or decoded_frames == 0:
            raise RuntimeError("Edge TTS 没有生成可播放的音频")
        tail = re.sub(r"\s+", " ", text).strip()[-24:]
        print(
            f"TTS 合成完成：{len(text)} 字，{len(segments)} 段，"
            f"音频 {decoded_frames / self._sample_rate:.2f} 秒；结尾：{tail}",
            flush=True,
        )


class PushToTalkController:
    def __init__(self, microphone_gate: threading.Event) -> None:
        self._microphone_gate = microphone_gate
        self._listener = None

    def start(self) -> None:
        from pynput import keyboard

        def on_press(key: object, *_: object) -> None:
            if key == keyboard.Key.f12:
                self._microphone_gate.set()

        def on_release(key: object, *_: object) -> None:
            if key == keyboard.Key.f12:
                self._microphone_gate.clear()

        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._listener = listener
        listener.start()
        listener.wait()
        if sys.platform == "darwin" and not listener.IS_TRUSTED:
            listener.stop()
            listener.join(timeout=2)
            self._listener = None
            raise RuntimeError(
                "无法监听 F12。请在 macOS 系统设置 > 隐私与安全性中，"
                "为当前终端开启“输入监控”；若仍不可用，再开启“辅助功能”，然后重启终端"
            )

    def stop(self) -> None:
        self._microphone_gate.clear()
        if self._listener is None:
            return
        self._listener.stop()
        self._listener.join(timeout=2)
        if self._listener.is_alive():
            raise RuntimeError("F12 按键监听器未能及时停止")
        self._listener = None


def apply_microphone_gate(
    frames: np.ndarray,
    current_gain: float,
    enabled: bool,
) -> tuple[np.ndarray, float]:
    target_gain = 1.0 if enabled else 0.0
    if current_gain == target_gain:
        return frames * target_gain, target_gain
    ramp = np.linspace(
        current_gain,
        target_gain,
        num=len(frames),
        dtype=np.float32,
    ).reshape(-1, 1)
    return frames * ramp, target_gain


class InteractiveAudioBridge:
    def __init__(
        self,
        mic_device_index: int | None,
        output_device_index: int,
        output_device_name: str,
        sample_rate: int,
        pcm_buffer: PCMBuffer,
        mic_gain: float,
        tts_gain: float,
        microphone_gate: threading.Event | None = None,
    ) -> None:
        self._mic_device_index = mic_device_index
        self._output_device_index = output_device_index
        self._output_device_name = output_device_name
        self._sample_rate = sample_rate
        self._pcm_buffer = pcm_buffer
        self._mic_gain = mic_gain
        self._tts_gain = tts_gain
        self._microphone_gate = microphone_gate
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="interactive-blackhole-bridge",
            daemon=True,
        )

    def start(self) -> None:
        if self._mic_device_index is not None:
            sd.check_input_settings(
                device=self._mic_device_index,
                channels=1,
                dtype="float32",
                samplerate=self._sample_rate,
            )
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("启动实时音频桥超时")
        if self._error is not None:
            raise RuntimeError(f"启动实时音频桥失败：{self._error}") from self._error

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise RuntimeError("实时音频桥未能及时停止")
        if self._error is not None:
            raise RuntimeError(f"实时音频桥运行失败：{self._error}") from self._error

    def _run(self) -> None:
        ffmpeg_process: subprocess.Popen[bytes] | None = None
        try:
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise RuntimeError("找不到 ffmpeg，请先安装：brew install ffmpeg")
            audiotoolbox_index = find_audiotoolbox_device(
                self._output_device_name,
                self._sample_rate,
            )
            ffmpeg_process = subprocess.Popen(
                [
                    ffmpeg,
                    "-v",
                    "error",
                    "-re",
                    "-f",
                    "f32le",
                    "-ar",
                    str(self._sample_rate),
                    "-ac",
                    str(CHANNELS),
                    "-i",
                    "pipe:0",
                    "-ar",
                    str(self._sample_rate),
                    "-ac",
                    str(CHANNELS),
                    "-c:a",
                    "pcm_f32le",
                    "-audio_device_index",
                    str(audiotoolbox_index),
                    "-f",
                    "audiotoolbox",
                    "-",
                ],
                stdin=subprocess.PIPE,
                bufsize=0,
            )
            assert ffmpeg_process.stdin is not None
            block_frames = max(1, self._sample_rate * INTERACTIVE_BLOCK_MS // 1000)
            block_index = 0
            tts_active = False
            microphone_gate_active = False
            microphone_gate_gain = 0.0 if self._microphone_gate is not None else 1.0
            block_seconds = block_frames / self._sample_rate
            next_deadline = time.monotonic()

            def pace_next_block() -> None:
                nonlocal next_deadline
                next_deadline += block_seconds
                delay = next_deadline - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                elif delay < -block_seconds:
                    next_deadline = time.monotonic()

            def write_block(mic_stereo: np.ndarray) -> None:
                nonlocal block_index, tts_active
                tts = self._pcm_buffer.read(block_frames) * self._tts_gain
                utterance_active = self._pcm_buffer.is_active()
                if utterance_active and not tts_active:
                    action = "混入" if self._mic_device_index is not None else "推送到"
                    print(f"\nTTS 开始{action} BlackHole", flush=True)
                elif tts_active and not utterance_active:
                    action = "混入" if self._mic_device_index is not None else "推送"
                    print(f"\nTTS {action}完成", flush=True)
                tts_active = utterance_active
                mixed = mic_stereo + tts
                peak = float(np.max(np.abs(mixed)))
                if peak > 0.98:
                    mixed *= 0.98 / peak
                ffmpeg_process.stdin.write(
                    np.ascontiguousarray(mixed, dtype="<f4").tobytes()
                )
                block_index += 1
                if block_index % 50 == 0:
                    actual_rate = get_sample_rate(self._output_device_index)
                    if actual_rate != self._sample_rate:
                        raise RuntimeError(
                            f"播放期间设备采样率发生变化："
                            f"{self._sample_rate} Hz -> {actual_rate} Hz"
                        )

            silence = np.zeros((block_frames, CHANNELS), dtype=np.float32)
            if self._mic_device_index is None:
                self._ready.set()
                while not self._stop.is_set():
                    write_block(silence)
                    pace_next_block()
            else:
                with sd.InputStream(
                    samplerate=self._sample_rate,
                    blocksize=block_frames,
                    channels=1,
                    dtype="float32",
                    device=self._mic_device_index,
                ) as mic_stream:
                    self._ready.set()
                    while not self._stop.is_set():
                        mic, overflowed = mic_stream.read(block_frames)
                        if overflowed:
                            print(
                                "\n警告：物理麦克风输入发生溢出",
                                file=sys.stderr,
                                flush=True,
                            )
                        mic_stereo = np.repeat(mic, CHANNELS, axis=1) * self._mic_gain
                        if self._microphone_gate is not None:
                            gate_enabled = self._microphone_gate.is_set()
                            if gate_enabled != microphone_gate_active:
                                status = "开启" if gate_enabled else "静音"
                                print(f"\nF12 对讲：麦克风已{status}", flush=True)
                                microphone_gate_active = gate_enabled
                            mic_stereo, microphone_gate_gain = apply_microphone_gate(
                                mic_stereo,
                                microphone_gate_gain,
                                gate_enabled,
                            )
                        write_block(mic_stereo)
                        pace_next_block()
            ffmpeg_process.stdin.close()
            return_code = ffmpeg_process.wait(timeout=5)
            if return_code != 0:
                raise RuntimeError(f"FFmpeg 音频桥退出码：{return_code}")
        except BaseException as exc:
            self._error = exc
            self._ready.set()
        finally:
            if ffmpeg_process is not None and ffmpeg_process.poll() is None:
                if ffmpeg_process.stdin is not None:
                    with contextlib.suppress(BrokenPipeError):
                        ffmpeg_process.stdin.close()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    ffmpeg_process.wait(timeout=2)
                if ffmpeg_process.poll() is None:
                    ffmpeg_process.terminate()


def run_interactive(
    args: argparse.Namespace,
    output_device_index: int,
    output_device_name: str,
    sample_rate: int,
) -> None:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.patch_stdout import patch_stdout

    if args.stream or args.verify:
        raise RuntimeError("--interactive 不能与 --stream 或 --verify 同时使用")
    if args.mic_gain < 0 or args.tts_gain < 0:
        raise RuntimeError("麦克风和 TTS 增益不能小于 0")

    use_microphone = args.interactive in {"mix", "ptt"}
    microphone_gate = threading.Event() if args.interactive == "ptt" else None
    ptt_controller = (
        PushToTalkController(microphone_gate)
        if microphone_gate is not None
        else None
    )
    mic_device_index = find_input_device(args.mic_device) if use_microphone else None
    mic_device_name = (
        str(sd.query_devices(mic_device_index)["name"])
        if mic_device_index is not None
        else None
    )
    pcm_buffer = PCMBuffer()
    tts_worker = TTSStreamWorker(
        pcm_buffer,
        sample_rate,
        args.voice,
        args.rate,
        args.volume,
    )
    bridge = InteractiveAudioBridge(
        mic_device_index,
        output_device_index,
        output_device_name,
        sample_rate,
        pcm_buffer,
        args.mic_gain,
        args.tts_gain,
        microphone_gate,
    )

    key_bindings = KeyBindings()

    @key_bindings.add("f12")
    def ignore_terminal_f12(_: object) -> None:
        pass

    prompt_session: PromptSession[str] = PromptSession(key_bindings=key_bindings)
    ptt_started = False
    bridge_started = False
    tts_started = False
    try:
        if ptt_controller is not None:
            ptt_controller.start()
            ptt_started = True
        bridge_started = True
        bridge.start()
        tts_worker.start()
        tts_started = True
        if mic_device_index is None:
            print("交互模式：仅控制台 TTS（不读取物理麦克风）")
        else:
            print(f"物理麦克风：{mic_device_name} (#{mic_device_index})")
            if microphone_gate is None:
                print("交互模式：控制台 TTS + 实时麦克风混合")
            else:
                print("交互模式：按住 F12 说话，松开后麦克风静音")
                print("MacBook 功能键用于调音量时，请按 Fn+F12。")
        print("输入文字后按回车合成，输入 /quit 退出。")
        if args.text:
            tts_worker.submit(args.text)
        with patch_stdout(raw=True):
            while True:
                try:
                    text = prompt_session.prompt("tts> ").strip()
                except EOFError:
                    break
                if text.casefold() in {"/quit", "/exit"}:
                    break
                if text:
                    tts_worker.submit(text)
    except KeyboardInterrupt:
        print()
    finally:
        if ptt_controller is not None and ptt_started:
            ptt_controller.stop()
        if tts_started:
            tts_worker.stop()
        if bridge_started:
            bridge.stop()
    print("交互输出已停止。")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="将 TTS 语音或音频文件推送到 BlackHole 虚拟麦克风")
    parser.add_argument("text", nargs="?", help="需要朗读的文本")
    parser.add_argument("--file", type=Path, help="播放本地音频文件（如 MP3、WAV、M4A、FLAC）")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="edge-tts 声音名称")
    parser.add_argument("--rate", default="+0%", help="语速，例如 +10%% 或 -10%%")
    parser.add_argument("--volume", default="+0%", help="音量，例如 +10%% 或 -10%%")
    parser.add_argument("--device", default="BlackHole 2ch", help="目标输出设备名称")
    parser.add_argument(
        "--mic-device",
        default="MacBook Pro麦克风",
        help="交互模式使用的物理麦克风名称",
    )
    parser.add_argument("--mic-gain", type=float, default=1.0, help="物理麦克风混音增益")
    parser.add_argument("--tts-gain", type=float, default=1.0, help="TTS 混音增益")
    parser.add_argument(
        "--interactive",
        nargs="?",
        const="mix",
        choices=INTERACTIVE_MODES,
        metavar="MODE",
        help=(
            "交互模式：console 仅控制台 TTS，mix 始终混合物理麦克风，"
            "ptt 按住 F12 时混合；默认 mix"
        ),
    )
    parser.add_argument("--stream", action="store_true", help="边合成边推送，不保存完整 MP3")
    parser.add_argument("--verify", action="store_true", help="同步回录并检查 BlackHole 信号")
    args = parser.parse_args(argv)
    if args.file is not None:
        if args.text is not None or args.interactive or args.stream:
            parser.error("--file 不能与文本、--interactive 或 --stream 同时使用")
        args.file = args.file.expanduser().resolve()
        if not args.file.is_file():
            parser.error(f"音频文件不存在或不是普通文件：{args.file}")
    return args


def verify_device_sample_rate(device_index: int, expected: int) -> None:
    actual = get_sample_rate(device_index)
    print(f"播放后设备采样率：{actual} Hz")
    if actual != expected:
        raise RuntimeError(f"播放期间设备采样率发生变化：{expected} Hz -> {actual} Hz")


def main() -> None:
    args = parse_args()
    device_index = find_device(args.device)
    device = sd.query_devices(device_index)
    device_name = device["name"]
    print("等待设备采样率稳定...")
    sample_rate = wait_for_stable_sample_rate(device_index)
    if sample_rate <= 0:
        raise RuntimeError(f"设备返回了无效采样率：{sample_rate}")
    if args.file is None:
        print(f"合成声音：{args.voice}")
    print(f"输出设备：{device_name} (#{device_index})")
    print(f"设备采样率：{sample_rate} Hz")
    if args.file is not None:
        print(f"音频文件：{args.file}")
        print(f"音频转换：文件音轨 -> {sample_rate} Hz")
    else:
        print(f"音频转换：Edge TTS {EDGE_TTS_SAMPLE_RATE} Hz -> {sample_rate} Hz")
    print("输出格式：PCM float32 / 双声道")
    if args.interactive:
        run_interactive(
            args,
            device_index,
            str(device_name),
            sample_rate,
        )
        verify_device_sample_rate(device_index, sample_rate)
        return
    text = args.text or DEFAULT_TEXT
    if args.stream:
        if args.verify:
            raise RuntimeError("流式模式暂不支持 --verify；腾讯会议使用期间也不应启用回录")
        asyncio.run(
            stream_to_blackhole(
                text,
                args.voice,
                args.rate,
                args.volume,
                device_index,
                str(device_name),
                sample_rate,
            )
        )
        verify_device_sample_rate(device_index, sample_rate)
        print("播放完成。")
        return
    if args.file is not None:
        dbfs = play(args.file, device_index, str(device_name), sample_rate, args.verify)
    else:
        with tempfile.TemporaryDirectory(prefix="edge-tts-blackhole-") as temp_dir:
            audio_file = Path(temp_dir) / "speech.mp3"
            asyncio.run(synthesize(text, args.voice, args.rate, args.volume, audio_file))
            dbfs = play(audio_file, device_index, str(device_name), sample_rate, args.verify)
    verify_device_sample_rate(device_index, sample_rate)
    print("播放完成。")
    if args.verify:
        if dbfs is None or dbfs < -60:
            raise RuntimeError(f"回录信号过低：{dbfs if dbfs is not None else '无数据'} dBFS")
        print(f"BlackHole 回录验证通过：{dbfs:.1f} dBFS")


if __name__ == "__main__":
    main()
