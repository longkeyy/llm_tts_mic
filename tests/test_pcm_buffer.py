import asyncio
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from tts_to_blackhole import (
    PCMBuffer,
    TTSStreamWorker,
    apply_microphone_gate,
    comparable_tts_text,
    main,
    parse_args,
    split_tts_text,
)


class PCMBufferTest(unittest.TestCase):
    def test_reads_across_chunks_and_zero_fills_underrun(self) -> None:
        buffer = PCMBuffer()
        buffer.append(np.array([[1, 10], [2, 20]], dtype=np.float32))
        buffer.append(np.array([[3, 30]], dtype=np.float32))

        np.testing.assert_array_equal(
            buffer.read(2),
            np.array([[1, 10], [2, 20]], dtype=np.float32),
        )
        np.testing.assert_array_equal(
            buffer.read(3),
            np.array([[3, 30], [0, 0], [0, 0]], dtype=np.float32),
        )

    def test_rejects_non_stereo_frames(self) -> None:
        buffer = PCMBuffer()
        with self.assertRaisesRegex(ValueError, "双声道"):
            buffer.append(np.ones((4, 1), dtype=np.float32))

    def test_utterance_stays_active_during_an_internal_silence(self) -> None:
        buffer = PCMBuffer()
        buffer.begin_utterance()

        np.testing.assert_array_equal(
            buffer.read(2),
            np.zeros((2, 2), dtype=np.float32),
        )
        self.assertTrue(buffer.is_active())

        buffer.end_utterance()
        self.assertFalse(buffer.is_active())


class TextChunkTest(unittest.TestCase):
    def test_chunks_long_multiline_text_without_losing_content(self) -> None:
        text = "第一周：模板锁定。\n* Day 1-2：提交样图。\n* 此后修改模板需额外评估工期"

        chunks = split_tts_text(text, max_chars=24)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 24 for chunk in chunks))
        self.assertEqual(
            comparable_tts_text(" ".join(chunks)),
            comparable_tts_text(text),
        )

    def test_incomplete_segment_is_retried(self) -> None:
        class FakeCommunicate:
            calls = 0

            def __init__(self, *_: object, **__: object) -> None:
                pass

            async def stream(self):
                FakeCommunicate.calls += 1
                yield {"type": "audio", "data": b"mp3"}
                boundary = (
                    "只返回前半段"
                    if FakeCommunicate.calls == 1
                    else "这是完整内容的结尾"
                )
                yield {"type": "SentenceBoundary", "text": boundary}

        async def no_sleep(_: float) -> None:
            pass

        worker = TTSStreamWorker(PCMBuffer(), 48000, "test-voice", "+0%", "+0%")
        with (
            patch("tts_to_blackhole.edge_tts.Communicate", FakeCommunicate),
            patch("tts_to_blackhole.asyncio.sleep", no_sleep),
        ):
            media = asyncio.run(
                worker._download_segment("这是完整内容的结尾", 1, 1)
            )

        self.assertEqual(media, b"mp3")
        self.assertEqual(FakeCommunicate.calls, 2)


class ParseArgsTest(unittest.TestCase):
    def test_file_mode_rejects_conflicting_sources(self) -> None:
        for extra in (["hello"], ["--interactive"], ["--stream"]):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    parse_args(["--file", "audio.wav", *extra])
                self.assertEqual(error.exception.code, 2)

    def test_file_mode_rejects_missing_file_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            for path in (folder, str(Path(folder) / "missing.wav")):
                with self.subTest(path=path), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parse_args(["--file", path])

    def test_file_mode_plays_selected_file_without_tts(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / "测试 audio.wav"
            audio.write_bytes(b"fixture")
            args = parse_args(["--file", str(audio), "--device", "Test Output", "--verify"])
            with (
                patch("tts_to_blackhole.parse_args", return_value=args),
                patch("tts_to_blackhole.find_device", return_value=3) as find,
                patch("tts_to_blackhole.sd.query_devices", return_value={"name": "Test Output"}),
                patch("tts_to_blackhole.wait_for_stable_sample_rate", return_value=48000),
                patch("tts_to_blackhole.verify_device_sample_rate") as verify_rate,
                patch("tts_to_blackhole.synthesize") as synthesize,
                patch("tts_to_blackhole.play", return_value=-20.0) as play,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                main()
            find.assert_called_once_with("Test Output")
            play.assert_called_once_with(audio.resolve(), 3, "Test Output", 48000, True)
            verify_rate.assert_called_once_with(3, 48000)
            synthesize.assert_not_called()

    def test_interactive_defaults_to_mix_for_backward_compatibility(self) -> None:
        self.assertEqual(parse_args(["--interactive"]).interactive, "mix")

    def test_interactive_console_mode(self) -> None:
        self.assertEqual(parse_args(["--interactive", "console"]).interactive, "console")

    def test_interactive_push_to_talk_mode(self) -> None:
        self.assertEqual(parse_args(["--interactive", "ptt"]).interactive, "ptt")


class MicrophoneGateTest(unittest.TestCase):
    def test_closed_gate_mutes_microphone(self) -> None:
        frames = np.ones((4, 2), dtype=np.float32)

        gated, gain = apply_microphone_gate(frames, current_gain=0.0, enabled=False)

        np.testing.assert_array_equal(gated, np.zeros((4, 2), dtype=np.float32))
        self.assertEqual(gain, 0.0)

    def test_gate_uses_one_block_fade(self) -> None:
        frames = np.ones((4, 2), dtype=np.float32)

        gated, gain = apply_microphone_gate(frames, current_gain=0.0, enabled=True)

        np.testing.assert_allclose(
            gated[:, 0],
            np.array([0.0, 1 / 3, 2 / 3, 1.0], dtype=np.float32),
        )
        self.assertEqual(gain, 1.0)

        gated, gain = apply_microphone_gate(frames, current_gain=gain, enabled=False)

        np.testing.assert_allclose(
            gated[:, 0],
            np.array([1.0, 2 / 3, 1 / 3, 0.0], dtype=np.float32),
        )
        self.assertEqual(gain, 0.0)


if __name__ == "__main__":
    unittest.main()
