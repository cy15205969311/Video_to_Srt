import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from subtitle_export import (
    PlaybackState,
    SubtitleConfig,
    ExportConfig,
    ass_color,
    build_ffmpeg_command,
    config_for_playback_state,
    convert_srt_to_ass,
    force_style_from_config,
    hex_to_ass_color,
    merge_export_config,
    parse_ffmpeg_progress,
    parse_ffmpeg_time,
    probe_video_height,
    resolution_scaled_config,
    sub_pos_to_margin_v,
    srt_to_ass,
)


class SubtitleExportTests(unittest.TestCase):
    def test_resolution_scaled_font_and_outline(self):
        scaled = resolution_scaled_config(
            SubtitleConfig(font_size=24, outline_width=2),
            video_height=1080,
        )
        self.assertEqual(scaled.font_size, 36)
        self.assertEqual(scaled.outline_width, 3)

    def test_config_for_playback_state_handles_empty_or_partial_state(self):
        gui = SubtitleConfig(font_size=22, margin_vertical=77)
        for state in (None, {}, {"audio_delay": 0.25}, {"sub_scale": 1.5}):
            current = config_for_playback_state(gui, state)
            self.assertEqual(current.font_size, 22 if state != {"sub_scale": 1.5} else 33)
            if state != {"sub_scale": 1.5}:
                self.assertEqual(current.margin_vertical, 77)

    def test_probe_video_height_reads_first_video_stream(self):
        completed = type("Completed", (), {"stdout": "1080\n", "stderr": ""})()
        with patch("subtitle_export.subprocess.run", return_value=completed) as run:
            self.assertEqual(probe_video_height("input.mp4", "ffprobe.exe"), 1080)
        command = run.call_args.args[0]
        self.assertIn("-select_streams", command)
        self.assertIn("v:0", command)

    def test_ass_color_uses_ass_bgr_and_inverse_alpha(self):
        self.assertEqual(ass_color("#12AB34"), "&H0034AB12&")
        self.assertEqual(ass_color("#8012AB34"), "&H7F34AB12&")
        self.assertEqual(hex_to_ass_color("#FF0000"), "&H000000FF&")

    def test_force_style_uses_bottom_center_and_gui_style(self):
        style = force_style_from_config(
            SubtitleConfig(
                font_name="SimHei",
                font_size=31,
                primary_color="#FF0000",
                outline_width=4,
                outline_color="#00FF00",
                background_enabled=True,
                background_color="#0000FF",
            )
        )
        self.assertIn("Fontname=SimHei", style)
        self.assertIn("Fontsize=31", style)
        self.assertIn("PrimaryColour=&H000000FF&", style)
        self.assertIn("Outline=4", style)
        self.assertIn("Alignment=2", style)

    def test_force_style_accepts_gui_mapping_names(self):
        # 面板使用 font_family 等语义化字段时，必须仍然映射到 ASS
        # 的 Fontname/颜色字段，而不是回退到默认样式。
        style = force_style_from_config(
            {
                "font_family": "Microsoft YaHei",
                "font_size": 28,
                "primary_color": "#123456",
                "outline_width": 3,
                "outline_color": "#654321",
                "background_enabled": False,
            }
        )
        self.assertIn("Fontname=Microsoft YaHei", style)
        self.assertIn("Fontsize=28", style)
        self.assertIn("PrimaryColour=&H00563412&", style)
        self.assertIn("Outline=3", style)

    def test_merge_export_config_is_single_snapshot(self):
        state = PlaybackState(sub_scale=1.5, sub_pos=25, sub_delay=-0.25, audio_delay=0.4)
        merged = merge_export_config(
            SubtitleConfig(font_name="SimHei", font_size=20),
            state,
            "video.mp4",
            "captions.srt",
            "rendered.mp4",
            video_height=1000,
        )
        self.assertIsInstance(merged, ExportConfig)
        self.assertEqual(merged.input_video, "video.mp4")
        self.assertEqual(merged.subtitle_path, "captions.srt")
        self.assertEqual(merged.output_video, "rendered.mp4")
        # 20px UI 字号 × 1.5 mpv 缩放 × (1000 / 720) 分辨率比例。
        self.assertEqual(merged.subtitle_config.font_size, 42)
        self.assertEqual(merged.subtitle_config.margin_vertical, 750)
        self.assertEqual(merged.audio_delay, 0.4)

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
        # 最终字号必须使用 GUI 基础字号 24，再乘 mpv 缩放 1.5。
        self.assertEqual(config.font_size, 36)
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

    def test_force_style_uses_ass_color_and_bottom_center(self):
        style = force_style_from_config(
            SubtitleConfig(
                font_name="SimHei",
                font_size=32,
                primary_color="#FF0000",
                outline_width=4,
                outline_color="#00FF00",
                background_enabled=True,
                background_color="#0000FF",
                background_opacity=128,
                position="top",
                margin_vertical=123,
            )
        )
        self.assertIn("Fontname=SimHei", style)
        self.assertIn("Fontsize=32", style)
        self.assertIn("PrimaryColour=&H000000FF&", style)
        self.assertIn("OutlineColour=&H0000FF00&", style)
        self.assertIn("Outline=4", style)
        self.assertIn("BackColour=&H7Fff0000&".lower(), style.lower())
        self.assertIn("Alignment=2", style)
        self.assertIn("MarginV=123", style)

    def test_merge_export_config_freezes_gui_and_mpv_once(self):
        plan = merge_export_config(
            SubtitleConfig(font_name="SimHei", font_size=20, primary_color="#112233"),
            PlaybackState(sub_scale=1.5, sub_pos=25, sub_delay=0.25, audio_delay=-0.5),
            "input.mp4",
            "captions.srt",
            "output.mp4",
            video_height=1000,
        )
        self.assertIsInstance(plan, ExportConfig)
        self.assertEqual(plan.input_video, "input.mp4")
        self.assertEqual(plan.subtitle_config.font_name, "SimHei")
        self.assertEqual(plan.subtitle_config.font_size, 42)
        self.assertEqual(plan.subtitle_config.margin_vertical, 750)
        self.assertEqual(plan.sub_delay, 0.25)
        self.assertEqual(plan.audio_delay, -0.5)

    def test_export_config_command_does_not_apply_scale_twice(self):
        plan = merge_export_config(
            SubtitleConfig(font_size=20),
            PlaybackState(sub_scale=1.5, sub_pos=100),
            "input.mp4",
            "captions.srt",
            "output.mp4",
        )
        command = build_ffmpeg_command(
            plan.input_video,
            "captions.ass",
            plan.output_video,
            ffmpeg_path="ffmpeg.exe",
            export_config=plan,
        )
        vf = command[command.index("-vf") + 1]
        self.assertIn("Fontsize=30", vf)
        self.assertNotIn("Fontsize=45", vf)

    def test_merge_without_mpv_preserves_gui_margin(self):
        gui = SubtitleConfig(font_size=22, margin_vertical=77)
        plan = merge_export_config(
            gui,
            None,
            "input.mp4",
            "captions.srt",
            "output.mp4",
        )
        self.assertEqual(plan.subtitle_config.margin_vertical, 77)
        gui.font_size = 99
        self.assertEqual(plan.subtitle_config.font_size, 22)


if __name__ == "__main__":
    unittest.main()
