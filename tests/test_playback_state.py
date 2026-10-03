import io
import json

from playback_state import MpvJsonIpc, PlaybackState, mpv_pos_to_ass_margin_v


def test_state_updates_and_listener_normalizes_values():
    state = PlaybackState(sub_pos=50)
    changes = []
    state.add_listener(lambda name, value, _: changes.append((name, value)))

    assert state.update_property("sub-delay", "0.25")
    assert state.update_property("audio-delay", -0.4)
    assert state.update_property("sub-scale", 1.5)
    assert state.update_property("sub-pos", 130)
    assert state.snapshot() == {
        "sub_delay": 0.25,
        "audio_delay": -0.4,
        "sub_scale": 1.5,
        "sub_font_size": None,
        "sub_pos": 100.0,
    }
    assert changes[-1] == ("sub-pos", 100.0)
    assert not state.update_property("unrelated", 1)


def test_state_persistence_preserves_unknown_mpv_properties(tmp_path):
    path = tmp_path / "persistent_config.json"
    path.write_text('{"volume": 101, "other": true}', encoding="utf-8")
    state = PlaybackState(sub_delay=0.8, audio_delay=-0.2, sub_scale=1.2, sub_pos=42)
    state.save(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["volume"] == 101
    assert data["other"] is True
    assert data["sub-delay"] == 0.8
    assert data["audio-delay"] == -0.2
    assert data["sub-pos"] == 42.0
    restored = PlaybackState.load(path)
    assert restored.snapshot()["sub_delay"] == 0.8
    assert restored.snapshot()["sub_pos"] == 42.0


def test_mpv_position_maps_linearly_to_ass_margin():
    assert mpv_pos_to_ass_margin_v(100, play_res_y=1080, font_size=24, base_margin=20) == 20
    assert mpv_pos_to_ass_margin_v(0, play_res_y=1080, font_size=24, base_margin=20) == 1036
    assert mpv_pos_to_ass_margin_v(50, play_res_y=1080, font_size=24, base_margin=20) == 528


def test_ipc_reader_updates_state_from_property_events():
    state = PlaybackState()
    ipc = MpvJsonIpc("tcp://127.0.0.1:1", state)
    payload = b"".join(
        (
            json.dumps({"event": "property-change", "name": "sub-scale", "data": 1.75}).encode(),
            b"\n",
            json.dumps({"event": "property-change", "name": "sub-pos", "data": 15}).encode(),
            b"\n",
        )
    )
    ipc._transport = io.BytesIO(payload)
    ipc._reader_loop()
    assert state.sub_scale == 1.75
    assert state.sub_pos == 15


def test_mpv_command_contains_json_ipc_endpoint():
    command = MpvJsonIpc.mpv_command("mpv.exe", "video.mp4", "tcp://127.0.0.1:1234", config_dir="portable_config")
    assert command[:3] == ["mpv.exe", "--input-ipc-server=tcp://127.0.0.1:1234", "--config-dir=portable_config"]
    assert command[-1] == "video.mp4"
