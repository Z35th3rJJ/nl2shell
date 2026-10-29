import unittest
import os
import json
import sys
import tempfile
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.voice_input import VoiceInputError, _prepare_cuda, confirm_transcript, record_and_transcribe

try:
    import numpy
    import opencc
except ImportError:
    numpy = None


class VoiceInputTests(unittest.TestCase):
    def setUp(self):
        self.voice_env = patch.dict(os.environ, {"NL2SHELL_VOICE_MODEL": "whisper"})
        self.voice_env.start()
        self.addCleanup(self.voice_env.stop)

    def test_missing_gpu_is_reported(self):
        module = SimpleNamespace(get_cuda_device_count=lambda: 0)
        with patch.dict(sys.modules, {"ctranslate2": module}):
            with self.assertRaisesRegex(VoiceInputError, "未检测到 NVIDIA GPU"):
                _prepare_cuda()

    def test_missing_gpu_runtime_is_reported(self):
        module = SimpleNamespace(get_cuda_device_count=lambda: 1)
        with patch.dict(sys.modules, {"ctranslate2": module}), \
             patch("core.voice_input.ctypes.CDLL", side_effect=OSError("not found")):
            with self.assertRaisesRegex(VoiceInputError, "GPU 运行库不可用"):
                _prepare_cuda()

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
             patch("core.voice_input._prepare_cuda"), \
             patch("core.voice_input.subprocess.Popen", side_effect=start), patch("builtins.print") as output:
            self.assertEqual(record_and_transcribe(lambda _: ""), "列出当前目录 sample.txt ERROR.*")
        output.assert_any_call("识别原文：列出當前目錄 sample.txt ERROR.*")
        output.assert_any_call("正在识别语音（模型：faster-whisper medium，NVIDIA GPU）；首次使用可能需要下载模型。")
        self.assertFalse(audio[0].exists())
        module.WhisperModel.assert_called_once_with("medium", device="cuda", compute_type="int8_float16")
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
             patch("core.voice_input._prepare_cuda"), \
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
             patch("core.voice_input._prepare_cuda"), \
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
             patch("core.voice_input._prepare_cuda"), \
             patch("core.voice_input.subprocess.Popen", return_value=process), patch("builtins.print"):
            self.assertIsNone(record_and_transcribe(lambda _: "/cancel"))
        process.send_signal.assert_called_once()
        module.WhisperModel.assert_not_called()

    @unittest.skipIf(numpy is None, "需要语音可选依赖")
    def test_sensevoice_transcribes_recording_on_cpu(self):
        process = Mock(returncode=0)
        process.poll.side_effect = [None, 0]
        stream = Mock()
        stream.result.text = '列出當前目錄 sample.txt'
        recognizer = Mock()
        recognizer.create_stream.return_value = stream
        factory = Mock(return_value=recognizer)
        module = SimpleNamespace(OfflineRecognizer=SimpleNamespace(from_sense_voice=factory))
        def recorder(argv, **kwargs):
            with wave.open(argv[-1], 'wb') as recording:
                recording.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                recording.writeframes(b'\x01\x00' * 800)
            return process
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_dir = root / '.local/share/nl2shell/sensevoice'
            model_dir.mkdir(parents=True)
            (model_dir / 'model.int8.onnx').touch()
            (model_dir / 'tokens.txt').touch()
            with patch.dict(os.environ, {'NL2SHELL_VOICE_MODEL': 'sensevoice'}), \
                 patch.dict(sys.modules, {'sherpa_onnx': module}), \
                 patch('core.voice_input.Path.home', return_value=root), \
                 patch('core.voice_input.subprocess.Popen', side_effect=recorder), patch('builtins.print'):
                text = record_and_transcribe(lambda _: '')
            self.assertEqual(text, '列出当前目录 sample.txt')
            self.assertEqual(factory.call_args.kwargs['provider'], 'cpu')
            self.assertEqual(factory.call_args.kwargs['language'], 'zh')
            stream.accept_waveform.assert_called_once()
            recognizer.decode_stream.assert_called_once_with(stream)

    @unittest.skipIf(numpy is None, "需要语音可选依赖")
    def test_sensevoice_missing_model_stops_before_recording(self):
        module = SimpleNamespace(OfflineRecognizer=Mock())
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {'NL2SHELL_VOICE_MODEL': 'sensevoice'}), \
             patch.dict(sys.modules, {'sherpa_onnx': module}), \
             patch('core.voice_input.Path.home', return_value=Path(directory)), \
             patch('core.voice_input.subprocess.Popen') as recorder:
            with self.assertRaisesRegex(VoiceInputError, '未安装 SenseVoiceSmall'):
                record_and_transcribe(lambda _: '')
        recorder.assert_not_called()

    @unittest.skipIf(numpy is None, "需要语音可选依赖")
    def test_qwen_worker_transcript_is_confirmable_and_simplified(self):
        process = Mock(returncode=0)
        process.poll.side_effect = [None, 0]

        def recorder(argv, **kwargs):
            with wave.open(argv[-1], "wb") as recording:
                recording.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                recording.writeframes(b"\x01\x00" * 800)
            return process

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / ".local/share/nl2shell/qwen-asr/bin/python"
            python.parent.mkdir(parents=True)
            python.touch()
            result = SimpleNamespace(stdout=json.dumps({"text": "列出當前目錄 sample.txt"}))
            with patch.dict(os.environ, {"NL2SHELL_VOICE_MODEL": "qwen"}), \
                 patch("core.voice_input.Path.home", return_value=root), \
                 patch("core.voice_input.subprocess.Popen", side_effect=recorder), \
                 patch("core.voice_input.subprocess.run", return_value=result) as run, patch("builtins.print") as output:
                text = record_and_transcribe(lambda _: "")
            self.assertEqual(text, "列出当前目录 sample.txt")
            self.assertEqual(run.call_args.args[0][0], str(python))
            output.assert_any_call("识别原文：列出當前目錄 sample.txt")

    def test_unrecognized_voice_model_is_rejected(self):
        with patch.dict(os.environ, {"NL2SHELL_VOICE_MODEL": "other"}):
            with self.assertRaisesRegex(VoiceInputError, "只能选择"):
                record_and_transcribe(lambda _: "")

    @unittest.skipIf(numpy is None, "需要语音可选依赖")
    def test_missing_qwen_environment_stops_before_recording(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"NL2SHELL_VOICE_MODEL": "qwen"}), \
             patch("core.voice_input.Path.home", return_value=Path(directory)), \
             patch("core.voice_input.subprocess.Popen") as recorder:
            with self.assertRaisesRegex(VoiceInputError, "未安装 Qwen3-ASR"):
                record_and_transcribe(lambda _: "")
        recorder.assert_not_called()
