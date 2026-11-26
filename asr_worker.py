# -*- coding: utf-8 -*-
"""ASR worker: runs inside the project venv (faster-whisper), driven by server.py.

stdin : {"files": ["/path/a.mp4", ...], "model": "small", "language": "zh"}
stdout: [{"ok": true, "file": "...", "text": "...", "segments": [...]}, ...]
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception as exc:
        json.dump([{"ok": False, "error": f"bad payload: {exc}"}], sys.stdout, ensure_ascii=False)
        return 0

    files = [f for f in (payload.get("files") or []) if f and os.path.isfile(f)]
    model_name = str(payload.get("model") or "small")
    language = str(payload.get("language") or "zh")

    # Model cache stays inside the project; mirror works in mainland China.
    os.environ.setdefault("HF_HOME", os.path.join(ROOT, "data", "huggingface"))
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    try:
        import av
        # av>=15 removed the `metadata_errors` kwarg that faster-whisper still passes.
        _orig_open = av.open

        def _av_open(*args, **kwargs):
            kwargs.pop("metadata_errors", None)
            return _orig_open(*args, **kwargs)

        av.open = _av_open
        from faster_whisper import WhisperModel
    except Exception as exc:
        json.dump([{"ok": False, "error": f"ASR runtime unavailable: {exc}"}], sys.stdout, ensure_ascii=False)
        return 0

    try:
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
    except Exception as exc:
        json.dump([{"ok": False, "error": f"model load failed: {exc}"}], sys.stdout, ensure_ascii=False)
        return 0

    results = []
    for path in files:
        try:
            segments, info = model.transcribe(
                path,
                language=language if language and language != "auto" else None,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
            )
            segs = []
            for seg in segments:
                text = (seg.text or "").strip()
                if text:
                    segs.append({"start": round(float(seg.start), 2), "end": round(float(seg.end), 2), "text": text})
            results.append({
                "ok": True,
                "file": path,
                "text": "".join(s["text"] for s in segs).strip(),
                "segments": segs[:800],
                "duration": round(float(info.duration or 0), 1),
                "language": info.language or "",
            })
        except Exception as exc:
            results.append({"ok": False, "file": path, "error": str(exc)})

    json.dump(results, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
