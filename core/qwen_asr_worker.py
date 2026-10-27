"""在独立环境中运行 Qwen3-ASR，进程退出后释放显存。"""
import json
import sys
from contextlib import redirect_stdout


def main():
    with redirect_stdout(sys.stderr):
        import torch
        from qwen_asr import Qwen3ASRModel

        if not torch.cuda.is_available():
            raise RuntimeError("未检测到可用的 NVIDIA GPU")
        model = Qwen3ASRModel.from_pretrained(
            "Qwen/Qwen3-ASR-0.6B", dtype=torch.float16, device_map="cuda:0",
            max_inference_batch_size=1, max_new_tokens=512,
        )
        results = model.transcribe(audio=sys.argv[1], language="Chinese")
    print(json.dumps({"text": results[0].text}, ensure_ascii=False))


if __name__ == "__main__":
    main()
