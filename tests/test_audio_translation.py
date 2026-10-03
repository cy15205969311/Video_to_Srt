import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from audio_translation import _english_chunks, _ffmpeg_path, translate_media_to_english_mp3
from videotosrt import _run_ffmpeg


class AudioTranslationTests(unittest.TestCase):
    def test_english_text_is_split_under_service_limit(self):
        chunks = _english_chunks(("hello " * 400).strip(), max_chars=900)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(chunk) <= 900 for chunk in chunks))

    def test_ffmpeg_invalid_console_bytes_do_not_raise_decode_error(self):
        command = [sys.executable, "-c", "import os; os.write(2, bytes([0xa5])); raise SystemExit(1)"]
        with self.assertRaises(RuntimeError) as error:
            _run_ffmpeg(command)
        self.assertIn("\ufffd", str(error.exception))

    def test_pipeline_translates_both_directions_and_merges_mp3(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            tts_fixture = root / "tts.mp3"
            subprocess.run(
                [_ffmpeg_path(), "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-ar", "16000", str(source)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                [_ffmpeg_path(), "-y", "-f", "lavfi", "-i", "sine=frequency=660:duration=0.4", str(tts_fixture)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            for source_language, target_language, dev_pid in (("zh", "en", 1537), ("en", "zh", 1737)):
                output = root / f"source-{target_language}.mp3"
                token_response = MagicMock()
                token_response.json.return_value = {"access_token": "test-token"}
                translation_response = MagicMock()
                translation_response.json.return_value = {
                    "result": {"trans_result": [{"dst": "Hello there." if target_language == "en" else "你好。"}]}
                }
                tts_response = MagicMock()
                tts_response.headers = {"Content-Type": "audio/mp3"}
                tts_response.content = tts_fixture.read_bytes()
                tts_response.text = ""

                with patch("audio_translation.find_speech_regions", return_value=[(0.0, 0.8, 1)]), \
                        patch("audio_translation.SpeechRecognizer") as recognizer_type, \
                        patch("audio_translation.requests.get", return_value=token_response), \
                        patch("audio_translation.requests.post", side_effect=[translation_response, tts_response]) as post:
                    recognizer_type.return_value.token = "test-token"
                    recognizer_type.return_value.return_value = "你好。" if source_language == "zh" else "Hello."
                    result = translate_media_to_english_mp3(
                        source,
                        output,
                        ("speech-ak", "speech-sk"),
                        ("translation-ak", "translation-sk"),
                        source_language=source_language,
                        target_language=target_language,
                    )

                self.assertEqual(Path(result), output)
                self.assertGreater(output.stat().st_size, 0)
                recognizer_type.assert_called_once_with("speech-ak", "speech-sk", 16000, "wav", dev_pid)
                self.assertEqual(post.call_args_list[0].kwargs["json"]["from"], source_language)
                self.assertEqual(post.call_args_list[0].kwargs["json"]["to"], target_language)
                self.assertEqual(post.call_args_list[1].kwargs["data"]["lan"], target_language)

                ffprobe_path = str(Path(_ffmpeg_path()).with_name("ffprobe.exe" if _ffmpeg_path().lower().endswith(".exe") else "ffprobe"))
                probe = subprocess.run(
                    [ffprobe_path, "-v", "error", "-show_entries", "format=format_name", "-of", "default=nw=1:nk=1", str(output)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertIn("mp3", probe.stdout.lower())


if __name__ == "__main__":
    unittest.main()
