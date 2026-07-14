"""Local audio transcription helpers for NVIDIA Parakeet TDT."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional


PARAKEET_MODEL_FILES = (
    "config.json",
    "encoder-model.int8.onnx",
    "decoder_joint-model.int8.onnx",
    "nemo128.onnx",
    "vocab.txt",
)


def _model_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("PARAKEET_MODEL_DIR")
    if configured:
        candidates.append(Path(configured).expanduser())

    home = Path.home()
    candidates.extend(
        [
            home
            / "Library/Application Support/com.pais.handy/models"
            / "parakeet-tdt-0.6b-v3-int8",
            home
            / "Library/Application Support/parakeet/models"
            / "parakeet-tdt-0.6b-v3",
        ]
    )
    return candidates


def find_model_dir(model_dir: Optional[str] = None) -> Path:
    """Find a compatible local Parakeet ONNX INT8 model directory."""
    candidates = [Path(model_dir).expanduser()] if model_dir else _model_candidates()
    for candidate in candidates:
        if all((candidate / filename).is_file() for filename in PARAKEET_MODEL_FILES):
            return candidate.resolve()

    checked = ", ".join(str(path) for path in candidates)
    raise RuntimeError(
        "Parakeet model not found. Run `parakeet download`, set "
        "PARAKEET_MODEL_DIR, or pass model_dir explicitly. "
        f"Checked: {checked}"
    )


def find_parakeet_binary() -> str:
    """Find the parakeet-cli executable."""
    configured = os.environ.get("PARAKEET_BIN")
    binary = configured or shutil.which("parakeet")
    if not binary:
        raise RuntimeError(
            "parakeet-cli is not installed or is not on PATH. "
            "See https://github.com/lucataco/parakeet-cli"
        )
    return binary


def _to_wav(audio_path: Path, output_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to transcribe non-WAV WhatsApp audio")

    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(audio_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown ffmpeg error"
        raise RuntimeError(f"Failed to convert WhatsApp audio to WAV: {detail}")


def transcribe_audio_file(
    audio_path: str,
    model_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Transcribe a local audio file with parakeet-cli and return its JSON result."""
    source = Path(audio_path).expanduser().resolve()
    if not source.is_file():
        raise RuntimeError(f"Audio file not found: {source}")

    binary = find_parakeet_binary()
    resolved_model_dir = find_model_dir(model_dir)

    with tempfile.TemporaryDirectory(prefix="whatsapp-mcp-parakeet-") as temp_dir:
        if source.suffix.lower() == ".wav":
            wav_path = source
        else:
            wav_path = Path(temp_dir) / "audio.wav"
            _to_wav(source, wav_path)

        result = subprocess.run(
            [
                binary,
                "transcribe",
                str(wav_path),
                "--model-dir",
                str(resolved_model_dir),
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Parakeet error"
        raise RuntimeError(f"Parakeet transcription failed: {detail}")

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Parakeet returned invalid JSON output") from exc

    text = payload.get("text")
    if not isinstance(text, str):
        raise RuntimeError("Parakeet response did not contain transcription text")

    return {
        "text": text,
        "audio_duration_seconds": payload.get("duration"),
        "inference_time_seconds": payload.get("inference_time"),
        "model_dir": str(resolved_model_dir),
    }
