import unittest
import sys
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.voice_input import VoiceInputError, confirm_transcript, record_and_transcribe

try:
    import numpy
    import opencc
except ImportError:
    numpy = None


class VoiceInputTests(unittest.TestCase):
    def test_confirm_edit_and_cancel(self):
        for answer, expected in (("yes", "复制 source.txt"), ("改成查看文件", "改成查看文件"), ("臺灣.txt", "臺灣.txt"), ("/cancel", None), ("", None)):
            with self.subTest(answer=answer):
                self.assertEqual(confirm_transcript("复制 source.txt", lambda _: answer), expected)

    def test_empty_recognition_never_becomes_a_task(self):
        with patch("builtins.print"):
            self.assertIsNone(confirm_transcript(" ", lambda _: self.fail("must not ask for confirmation")))

    def test_cli_only_submits_the_user_confirmed_text(self):
        import cli
        from core.settings import AppSettings

        session = Mock()
        session.prompt.side_effect = ["/voice", "更正后的请求", "/exit"]
        with patch.object(cli, "load_settings", return_value=AppSettings("confirm", True)), \
             patch.object(cli, "first_run_setup", side_effect=lambda settings: settings), \
             patch.object(cli, "create_executor"), patch.object(cli, "Engine"), \
             patch.object(cli, "HistoryStore"), patch.object(cli, "print_diagnostics"), \
             patch.object(cli, "recover_working_directory", return_value=(".", "")), \
             patch.object(cli, "record_and_transcribe", return_value="错误识别结果") as recorder, \
             patch.object(cli, "execute_request") as execute, patch("builtins.print"):
            cli.main(input_session=session)
        self.assertEqual(execute.call_count, 1)
        recorder.assert_called_once_with(session.prompt)
        self.assertEqual(execute.call_args.args[3], "更正后的请求")

    def test_cli_cancel_never_submits_task(self):
        import cli
        from core.settings import AppSettings

        session = Mock()
        session.prompt.side_effect = ["/voice", "/cancel", "/exit"]
        with patch.object(cli, "load_settings", return_value=AppSettings("confirm", True)), \
             patch.object(cli, "first_run_setup", side_effect=lambda settings: settings), \
             patch.object(cli, "create_executor"), patch.object(cli, "Engine"), \
             patch.object(cli, "HistoryStore"), patch.object(cli, "print_diagnostics"), \
             patch.object(cli, "recover_working_directory", return_value=(".", "")), \
             patch.object(cli, "record_and_transcribe", return_value="复制文件"), \
             patch.object(cli, "execute_request") as execute, patch("builtins.print"):
            cli.main(input_session=session)
        execute.assert_not_called()

    @unittest.skipIf(numpy is None, "需要语音可选依赖")
    def test_recorder_stops_and_removes_temporary_audio(self):
        model = Mock()
        model.transcribe.return_value = ([SimpleNamespace(text="列出當前目錄 sample.txt ERROR.*")], None)
        module = SimpleNamespace(WhisperModel=Mock(return_value=model))
        process = Mock(returncode=1)
        process.poll.side_effect = [None, 0]
        audio = []

        def start(argv, **kwargs):
            audio.append(Path(argv[-1]))
            with wave.open(str(audio[-1]), "wb") as recording:
                recording.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                recording.writeframes(b"\x01\x00" * (16000 * 12))
            return process

        with patch.dict(sys.modules, {"faster_whisper": module}), \
             patch("core.voice_input.subprocess.Popen", side_effect=start), patch("builtins.print") as output:
            self.assertEqual(record_and_transcribe(lambda _: ""), "列出当前目录 sample.txt ERROR.*")
        output.assert_any_call("识别原文：列出當前目錄 sample.txt ERROR.*")
        output.assert_any_call("正在识别语音（模型：faster-whisper medium，CPU）；首次使用可能需要下载模型。")
        self.assertFalse(audio[0].exists())
        module.WhisperModel.assert_called_once_with("medium", device="cpu", compute_type="int8")
        process.send_signal.assert_called_once()
        pcm = model.transcribe.call_args.args[0]
        self.assertEqual(str(pcm.dtype), "float32")
        self.assertEqual(len(pcm), 16000 * 12)
        self.assertAlmostEqual(float(pcm[0]), 1 / 32768)

    @unittest.skipIf(numpy is None, "需要语音可选依赖")
    def test_failed_recorder_does_not_call_model(self):
        module = SimpleNamespace(WhisperModel=Mock())
        process = Mock(returncode=1)
        process.poll.return_value = 1
        with patch.dict(sys.modules, {"faster_whisper": module}), \
             patch("core.voice_input.subprocess.Popen", return_value=process), patch("builtins.print"):
            with self.assertRaises(VoiceInputError):
                record_and_transcribe(lambda _: "")
        module.WhisperModel.assert_not_called()

    @unittest.skipIf(numpy is None, "需要语音可选依赖")
    def test_silent_audio_does_not_call_model(self):
        module = SimpleNamespace(WhisperModel=Mock())
        process = Mock(returncode=1)
        process.poll.side_effect = [None, 1]

        def start(argv, **kwargs):
            with wave.open(argv[-1], "wb") as recording:
                recording.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                recording.writeframes(b"\x00\x00" * 800)
            return process

        with patch.dict(sys.modules, {"faster_whisper": module}), \
             patch("core.voice_input.subprocess.Popen", side_effect=start), patch("builtins.print"):
            with self.assertRaisesRegex(VoiceInputError, "没有声音"):
                record_and_transcribe(lambda _: "")
        module.WhisperModel.assert_not_called()

    @unittest.skipIf(numpy is None, "需要语音可选依赖")
    def test_cancel_recording_does_not_call_model(self):
        module = SimpleNamespace(WhisperModel=Mock())
        process = Mock(returncode=0)
        process.poll.side_effect = [None, 0]
        with patch.dict(sys.modules, {"faster_whisper": module}), \
             patch("core.voice_input.subprocess.Popen", return_value=process), patch("builtins.print"):
            self.assertIsNone(record_and_transcribe(lambda _: "/cancel"))
        process.send_signal.assert_called_once()
        module.WhisperModel.assert_not_called()
