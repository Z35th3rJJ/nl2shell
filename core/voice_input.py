"""可选的本地语音输入；识别结果需要用户确认。"""
import signal
import subprocess
import tempfile
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


def record_and_transcribe():
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise VoiceInputError("未安装语音依赖，请按 README 安装 voice 可选依赖。") from error

    with tempfile.TemporaryDirectory(prefix="nl2shell-voice-") as directory:
        audio = Path(directory) / "input.wav"
        print("开始录音，最长 8 秒；请在提示后说话。")
        try:
            process = subprocess.Popen(
                ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", str(audio)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            raise VoiceInputError("无法启动录音，请检查 PipeWire 和麦克风。") from error
        try:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired as error:
                    raise VoiceInputError("录音程序未能正常停止，请改用文字输入。") from error
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        if process.returncode not in (0, -signal.SIGINT) or not audio.exists() or audio.stat().st_size <= 44:
            raise VoiceInputError("录音失败，请检查麦克风，或改用文字输入。")
        print("正在识别语音；首次使用可能需要下载模型。")
        try:
            model = WhisperModel("base", device="cpu", compute_type="int8")
            segments, _ = model.transcribe(str(audio), language="zh", beam_size=1, vad_filter=True)
            return "".join(segment.text for segment in segments).strip()
        except Exception as error:
            raise VoiceInputError(f"语音识别失败：{error}") from error
