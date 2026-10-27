"""可选的本地语音输入；识别结果需要用户确认。"""
import ctypes
import importlib.metadata
import json
import os
import signal
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path


class VoiceInputError(Exception):
    pass


def _prepare_cuda():
    import ctranslate2

    try:
        if ctranslate2.get_cuda_device_count() < 1:
            raise VoiceInputError("未检测到 NVIDIA GPU，请检查 WSL 的 CUDA 环境。")
        for package, names in (
            ("nvidia-cublas-cu12", ("libcublasLt.so.12", "libcublas.so.12")),
            ("nvidia-cudnn-cu12", ("libcudnn.so.9",)),
        ):
            try:
                dist = importlib.metadata.distribution(package)
            except importlib.metadata.PackageNotFoundError:
                dist = None
            for name in names:
                files = [file for file in (dist.files or ()) if file.name == name] if dist else []
                path = str(dist.locate_file(files[0])) if files else name
                ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
    except (OSError, RuntimeError) as error:
        raise VoiceInputError("GPU 运行库不可用，请按 README 安装语音 GPU 依赖。") from error


def confirm_transcript(text, prompt):
    text = text.strip()
    if not text:
        print("没有识别到语音，请重新录制或输入文字。")
        return None
    print(f"识别结果：{text}")
    answer = prompt("输入 yes 使用识别结果，输入完整文字修改，或输入 /cancel 取消 > ").strip()
    if answer.lower() == "yes":
        return text
    if not answer or answer == "/cancel":
        return None
    return answer


def record_and_transcribe(prompt=input):
    voice_model = os.environ.get("NL2SHELL_VOICE_MODEL", "qwen").strip().lower()
    if voice_model not in {"qwen", "whisper"}:
        raise VoiceInputError("语音模型只能选择 qwen 或 whisper。")
    try:
        import numpy as np
        from opencc import OpenCC
        if voice_model == "whisper":
            from faster_whisper import WhisperModel
    except ImportError as error:
        raise VoiceInputError("未安装语音依赖，请按 README 安装 voice 可选依赖。") from error
    if voice_model == "whisper":
        _prepare_cuda()
    else:
        qwen_python = Path.home() / ".local/share/nl2shell/qwen-asr/bin/python"
        if not qwen_python.is_file():
            raise VoiceInputError("未安装 Qwen3-ASR 独立环境，请按 README 安装。")

    with tempfile.TemporaryDirectory(prefix="nl2shell-voice-") as directory:
        audio = Path(directory) / "input.wav"
        if shutil.which("parec"):
            command = ["parec", "--rate=16000", "--channels=1", "--format=s16le", "--file-format=wav", str(audio)]
        else:
            command = ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", str(audio)]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            raise VoiceInputError("无法启动录音，请检查 PulseAudio 或 PipeWire 和麦克风。") from error
        try:
            while True:
                answer = prompt("开始录音，请说话；按回车结束，输入 /cancel 取消 > ").strip()
                if not answer or answer == "/cancel":
                    break
                print("请按回车结束录音，或输入 /cancel 取消。")
            if process.poll() is not None:
                raise VoiceInputError("录音程序已停止，请检查麦克风，或改用文字输入。")
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as error:
                raise VoiceInputError("录音程序未能正常停止，请改用文字输入。") from error
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        if answer == "/cancel":
            return None
        if not audio.exists():
            raise VoiceInputError("录音失败，请检查麦克风，或改用文字输入。")
        try:
            with wave.open(str(audio), "rb") as recording:
                if (recording.getnchannels(), recording.getsampwidth(), recording.getframerate()) != (1, 2, 16000):
                    raise VoiceInputError("录音格式不受支持，请改用文字输入。")
                samples = recording.readframes(recording.getnframes())
                if not samples:
                    raise VoiceInputError("没有检测到录音，请检查麦克风，或改用文字输入。")
                if not any(samples):
                    raise VoiceInputError("录音没有声音，请检查麦克风，或改用文字输入。")
        except (OSError, wave.Error, EOFError) as error:
            raise VoiceInputError("录音文件无效，请改用文字输入。") from error
        try:
            if voice_model == "whisper":
                model_name = "medium"
                print(f"正在识别语音（模型：faster-whisper {model_name}，NVIDIA GPU）；首次使用可能需要下载模型。")
                model = WhisperModel(model_name, device="cuda", compute_type="int8_float16")
                pcm = np.frombuffer(samples, dtype="<i2").astype(np.float32) / 32768.0
                segments, _ = model.transcribe(pcm, language="zh", beam_size=1, vad_filter=True)
                text = "".join(segment.text for segment in segments).strip()
            else:
                print("正在识别语音（模型：Qwen3-ASR-0.6B，NVIDIA GPU）；首次使用可能需要下载模型。")
                worker = Path(__file__).with_name("qwen_asr_worker.py")
                result = subprocess.run([str(qwen_python), str(worker), str(audio)],
                                        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True)
                text = json.loads(result.stdout)["text"].strip()
            simplified = OpenCC("t2s").convert(text)
            if simplified != text:
                print(f"识别原文：{text}")
                print("识别文字已转为简体；如果文件名使用繁体，请输入原始文字。")
            return simplified
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or "").strip().splitlines()
            raise VoiceInputError("Qwen3-ASR 识别失败：" + (detail[-1] if detail else "请检查 GPU 和模型环境")) from error
        except Exception as error:
            raise VoiceInputError(f"语音识别失败：{error}") from error
