import tempfile
import unittest
from pathlib import Path

from subtitle_export import (
    PlaybackState,
    SubtitleConfig,
    ass_color,
    build_ffmpeg_command,
    config_for_playback_state,
    convert_srt_to_ass,
    parse_ffmpeg_progress,
    parse_ffmpeg_time,
    sub_pos_to_margin_v,
    srt_to_ass,
)


class SubtitleExportTests(unittest.TestCase):
    def test_ass_color_uses_ass_bgr_and_inverse_alpha(self):
        self.assertEqual(ass_color("#12AB34"), "&H0034AB12&")
        self.assertEqual(ass_color("#8012AB34"), "&H7F34AB12&")

    def test_srt_to_ass_keeps_multiline_and_style(self):
        source = "1\n00:00:01,000 --> 00:00:02,500\n第一行\n第二行\n"
        ass = srt_to_ass(source, SubtitleConfig(font_name="SimHei", font_size=30, position="top"))
        self.assertIn("Style: Default,SimHei,30", ass)
        self.assertIn("Dialogue: 0,0:00:01.00,0:00:02.50,Default", ass)
        self.assertIn(r"第一行\N第二行", ass)
        self.assertIn("Alignment", ass)

    def test_convert_srt_to_ass_writes_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            srt = root / "captions.srt"
            ass = root / "out" / "captions.ass"
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
            self.assertEqual(convert_srt_to_ass(srt, ass), str(ass))
            self.assertTrue(ass.is_file())
            self.assertIn("hello", ass.read_text(encoding="utf-8-sig"))

    def test_parse_progress_supports_time_and_progress_pipe(self):
        self.assertEqual(parse_ffmpeg_time("00:01:02.50"), 62.5)
        self.assertEqual(parse_ffmpeg_progress("frame=20 time=00:00:05.00", 10), 50)
        self.assertEqual(parse_ffmpeg_progress("out_time_ms=7500000", 10), 75)
        self.assertEqual(parse_ffmpeg_progress("frame=25", None, total_frames=100), 25)

    def test_command_escapes_windows_path_and_keeps_audio(self):
        command = build_ffmpeg_command(
            "input.mp4",
            r"C:\Videos\my captions.ass",
            "output.mp4",
            ffmpeg_path="ffmpeg.exe",
            progress_pipe=True,
        )
        self.assertEqual(command[0], "ffmpeg.exe")
        self.assertIn("-vf", command)
        vf = command[command.index("-vf") + 1]
        self.assertIn(r"C\:/Videos/my captions.ass", vf)
        self.assertIn("-c:a", command)
        self.assertIn("copy", command)
        self.assertIn("-progress", command)

    def test_playback_state_scales_font_and_maps_position(self):
        state = PlaybackState(sub_scale=1.5, sub_font_size=20, sub_pos=25)
        config = config_for_playback_state(
            SubtitleConfig(font_size=24, play_res_y=1000), state
        )
        self.assertEqual(config.font_size, 30)
        # mpv 的 sub-pos 从顶部计数，ASS 底部 MarginV 需要反向换算：
        # (100 - 25) / 100 * PlayResY = 750。
        self.assertEqual(config.margin_vertical, 750)
        self.assertEqual(sub_pos_to_margin_v(100, 1000), 0)
        self.assertEqual(sub_pos_to_margin_v(0, 1000), 1000)

    def test_subtitle_delay_shifts_ass_events_and_drops_past_events(self):
        source = (
            "1\n00:00:00,100 --> 00:00:00,500\nold\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\nkeep\n"
        )
        ass = srt_to_ass(source, subtitle_delay=-0.75)
        self.assertNotIn(",old", ass)
        self.assertIn("Dialogue: 0,0:00:00.25,0:00:01.25,Default", ass)

    def test_command_maps_playback_state_audio_delay_and_force_style(self):
        state = PlaybackState(sub_scale=2, sub_pos=50, audio_delay=0.25)
        command = build_ffmpeg_command(
            "input.mp4",
            "captions.ass",
            "output.mp4",
            ffmpeg_path="ffmpeg.exe",
            playback_state=state,
        )
        self.assertIn("-filter_complex", command)
        audio_filter = command[command.index("-filter_complex") + 1]
        self.assertIn("adelay=250:all=1", audio_filter)
        vf = command[command.index("-vf") + 1]
        self.assertIn("force_style=", vf)
        self.assertIn("Fontsize=48", vf)
        self.assertIn("MarginV=540", vf)

    def test_command_maps_negative_audio_delay_to_trim(self):
        command = build_ffmpeg_command(
            "input.mp4",
            "captions.ass",
            "output.mp4",
            audio_delay=-1.25,
        )
        audio_filter = command[command.index("-filter_complex") + 1]
        self.assertIn("atrim=start=1.250000", audio_filter)


if __name__ == "__main__":
    unittest.main()
