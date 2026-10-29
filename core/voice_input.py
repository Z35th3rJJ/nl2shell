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

from .bash_queries import _path
from .operations import workspace_path
from .requirements import path_value, transfer_expression


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


def _confirm_transfer_paths(text, cwd, prompt, excluded, evidence):
    # shortcut: 只处理共享解析器能完整识别的单源复制、移动；复杂请求保留整句修改入口。
    request = transfer_expression(text.rstrip("。！？"))
    if request is None:
        return text
    source = path_value(request["source"])
    requested = None
    try:
        root = Path(cwd).resolve(strict=True)
        requested = workspace_path(root, source)
        _path(root, str(requested), excluded)
        return text
    except FileNotFoundError as error:
        if requested is None:
            evidence["path_error"] = str(error)
            print("工作目录已不可用，请取消后重新选择工作目录。")
            return None
    except (OSError, ValueError) as error:
        evidence["path_error"] = str(error)
        print(f"源路径无法通过检查：{error}；请修改请求或取消。")
        return None

    print("未找到源对象：" + json.dumps(source, ensure_ascii=False))
    candidates = []
    try:
        parent = _path(root, str(requested.parent), excluded)
        for index, entry in enumerate(parent.iterdir()):
            if index >= 200:
                candidates = []
                print("目录项目较多，请手动输入源路径。")
                break
            try:
                candidates.append(_path(root, str(entry), excluded))
            except (OSError, ValueError):
                continue
        if len(candidates) > 20:
            candidates = []
            print("可选对象较多，请手动输入源路径。")
    except (OSError, ValueError):
        print("无法列出该源目录，请手动输入源路径。")
    candidates.sort(key=lambda path: (not path.is_file(), path.name))
    print("以下为该目录中的现有对象，请自行核对：")
    for index, path in enumerate(candidates, 1):
        print(f"{index}. " + json.dumps(path.relative_to(root).as_posix(), ensure_ascii=False))
    print("0. 手动输入源路径")
    while True:
        answer = prompt("请选择编号，或输入 /cancel 取消、/exit 退出 > ").strip()
        if not answer or answer == "/cancel":
            return None
        if answer == "/exit":
            return answer
        if answer == "0":
            value = prompt("输入工作目录内已有的源路径，或输入 /cancel 取消 > ").strip()
            if not value or value == "/cancel":
                return None
            if value == "/exit":
                return value
            value, method = path_value(value), "manual"
        elif answer in {str(index) for index in range(1, len(candidates) + 1)}:
            value, method = str(candidates[int(answer) - 1]), "candidate"
        else:
            print("请选择有效编号，或输入 0 手动指定源路径。")
            continue
        try:
            selected = _path(root, value, excluded).relative_to(root).as_posix()
        except (OSError, ValueError) as error:
            print(f"源路径无法通过检查：{error}")
            continue
        target = path_value(request["target"])
        print("目标位置（按原转写解析）：" + json.dumps(target, ensure_ascii=False))
        answer = prompt("输入目标路径修改，直接回车保留，或输入 /cancel 取消、/exit 退出 > ").strip()
        if answer == "/cancel":
            return None
        if answer == "/exit":
            return answer
        target = path_value(answer) if answer else target
        try:
            workspace_path(root, target)
        except (OSError, ValueError) as error:
            evidence["path_error"] = str(error)
            print(f"目标路径无法通过检查：{error}")
            return None
        corrected = text
        for field, value in (("target", target), ("source", selected)):
            quotes = next(((a, b) for a, b in (("\"", "\""), ("'", "'"), ("“", "”"), ("‘", "’"))
                           if a not in value and b not in value), None)
            if quotes is None:
                print("该路径无法用受支持的配对引号表示，请取消后修改完整请求。")
                return None
            start, end = request.span(field)
            corrected = corrected[:start] + quotes[0] + value + quotes[1] + corrected[end:]
        evidence.setdefault("source_corrections", []).append({
            "original_source": source, "selected_source": selected, "method": method})
        if target != path_value(request["target"]):
            evidence.setdefault("target_corrections", []).append({
                "original_target": path_value(request["target"]), "confirmed_target": target})
        return corrected


def confirm_transcript(text, prompt, *, cwd=None, excluded=(), evidence=None):
    text = text.strip()
    evidence = evidence if evidence is not None else {}
    evidence["transcript"] = text
    if not text:
        print("没有识别到语音，请重新录制或输入文字。")
        return None
    print(f"识别结果：{text}")
    while True:
        answer = prompt("输入 yes 使用以上文字，输入完整文字修改，或输入 /cancel 取消 > ").strip()
        if not answer or answer == "/cancel":
            return None
        confirmed = text if answer.lower() == "yes" else answer
        resolved = _confirm_transfer_paths(confirmed, cwd, prompt, excluded, evidence) if cwd is not None else confirmed
        if resolved is None:
            return None
        if resolved != confirmed and resolved != "/exit":
            text = resolved
            print("修订后的请求：" + text)
            continue
        evidence["confirmed_text"] = resolved
        return resolved


def record_and_transcribe(prompt=input):
    voice_model = os.environ.get("NL2SHELL_VOICE_MODEL", "sensevoice").strip().lower()
    if voice_model not in {"sensevoice", "qwen", "whisper"}:
        raise VoiceInputError("语音模型只能选择 sensevoice、qwen 或 whisper。")
    try:
        import numpy as np
        from opencc import OpenCC
        if voice_model == "whisper":
            from faster_whisper import WhisperModel
        elif voice_model == "sensevoice":
            import sherpa_onnx
    except ImportError as error:
        raise VoiceInputError("未安装语音依赖，请按 README 安装 voice 可选依赖。") from error
    if voice_model == "whisper":
        _prepare_cuda()
    elif voice_model == "qwen":
        qwen_python = Path.home() / ".local/share/nl2shell/qwen-asr/bin/python"
        if not qwen_python.is_file():
            raise VoiceInputError("未安装 Qwen3-ASR 独立环境，请按 README 安装。")
    else:
        model_dir = Path.home() / ".local/share/nl2shell/sensevoice"
        if not all((model_dir / name).is_file() for name in ("model.int8.onnx", "tokens.txt")):
            raise VoiceInputError("未安装 SenseVoiceSmall INT8 模型，请按 README 安装。")

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
            elif voice_model == "qwen":
                print("正在识别语音（模型：Qwen3-ASR-0.6B，NVIDIA GPU）；首次使用可能需要下载模型。")
                worker = Path(__file__).with_name("qwen_asr_worker.py")
                result = subprocess.run([str(qwen_python), str(worker), str(audio)],
                                        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True)
                text = json.loads(result.stdout)["text"].strip()
            else:
                print("正在识别语音（模型：SenseVoiceSmall INT8，CPU）。")
                model = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=str(model_dir / "model.int8.onnx"), tokens=str(model_dir / "tokens.txt"),
                    num_threads=4, language="zh", use_itn=True, provider="cpu")
                stream = model.create_stream()
                pcm = np.frombuffer(samples, dtype="<i2").astype(np.float32) / 32768.0
                stream.accept_waveform(16000, pcm)
                model.decode_stream(stream)
                text = stream.result.text.strip()
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
