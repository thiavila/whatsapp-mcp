import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import transcription


class TranscriptionTests(unittest.TestCase):
    def _create_model(self, root: str) -> Path:
        model_dir = Path(root) / "model"
        model_dir.mkdir()
        for filename in transcription.PARAKEET_MODEL_FILES:
            (model_dir / filename).touch()
        return model_dir

    def test_find_model_dir_uses_explicit_compatible_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = self._create_model(temp_dir)
            self.assertEqual(
                transcription.find_model_dir(str(model_dir)),
                model_dir.resolve(),
            )

    def test_find_model_dir_rejects_incomplete_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(RuntimeError, "Parakeet model not found"):
                transcription.find_model_dir(temp_dir)

    @patch("transcription.subprocess.run")
    @patch("transcription.find_parakeet_binary", return_value="/opt/bin/parakeet")
    def test_transcribes_wav_and_parses_json(self, _find_binary, run):
        run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {"text": "Oi, tudo bem?", "duration": 2.4, "inference_time": 0.1}
            ),
            stderr="",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = self._create_model(temp_dir)
            audio_path = Path(temp_dir) / "voice.wav"
            audio_path.touch()

            result = transcription.transcribe_audio_file(
                str(audio_path), model_dir=str(model_dir)
            )

        self.assertEqual(result["text"], "Oi, tudo bem?")
        self.assertEqual(result["audio_duration_seconds"], 2.4)
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["/opt/bin/parakeet", "transcribe"])
        self.assertIn("--format", command)

    @patch("transcription.shutil.which")
    @patch("transcription.subprocess.run")
    def test_converts_non_wav_with_ffmpeg(self, run, which):
        which.return_value = "/opt/bin/ffmpeg"
        run.return_value = MagicMock(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "voice.ogg"
            output = Path(temp_dir) / "voice.wav"
            source.touch()
            transcription._to_wav(source, output)

        command = run.call_args.args[0]
        self.assertEqual(command[0], "/opt/bin/ffmpeg")
        self.assertIn("16000", command)
        self.assertIn("1", command)


if __name__ == "__main__":
    unittest.main()
