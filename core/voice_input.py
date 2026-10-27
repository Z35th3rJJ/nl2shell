"""可选的本地语音输入；识别结果需要用户确认。"""
import signal
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path


class VoiceInputError(Exception):
    pass


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
    try:
        from faster_whisper import WhisperModel
        import numpy as np
        from opencc import OpenCC
    except ImportError as error:
        raise VoiceInputError("未安装语音依赖，请按 README 安装 voice 可选依赖。") from error

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
        model_name = "medium"
        print(f"正在识别语音（模型：faster-whisper {model_name}，CPU）；首次使用可能需要下载模型。")
        try:
            model = WhisperModel(model_name, device="cpu", compute_type="int8")
            pcm = np.frombuffer(samples, dtype="<i2").astype(np.float32) / 32768.0
            segments, _ = model.transcribe(pcm, language="zh", beam_size=1, vad_filter=True)
            text = "".join(segment.text for segment in segments).strip()
            simplified = OpenCC("t2s").convert(text)
            if simplified != text:
                print(f"识别原文：{text}")
                print("识别文字已转为简体；如果文件名使用繁体，请输入原始文字。")
            return simplified
        except Exception as error:
            raise VoiceInputError(f"语音识别失败：{error}") from error
