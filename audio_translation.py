"""Translate speech in a media file into a synthesized MP3."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import requests

from videotosrt import SpeechRecognizer, find_speech_regions


BAIDU_TOKEN_URL = "https://openapi.baidu.com/oauth/2.0/token"
BAIDU_TTS_URL = "https://tsn.baidu.com/text2audio"
LANGUAGE_LABELS = {"zh": "中文", "en": "英文"}


def _ffmpeg_path():
    executable = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    bundled = Path(__file__).resolve().parent / "ffmpeg-6.0-essentials_build" / "bin" / executable
    if bundled.is_file():
        return str(bundled)
    return executable


def _run_ffmpeg(args):
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run(
        [_ffmpeg_path(), "-hide_banner", "-loglevel", "error", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    if result.returncode:
        detail = result.stderr.strip() or "FFmpeg 处理失败"
        raise RuntimeError(detail)


def _translation_token(ak, sk):
    response = requests.get(
        BAIDU_TOKEN_URL,
        params={"grant_type": "client_credentials", "client_id": ak, "client_secret": sk},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("无法获取机器翻译令牌，请检查机器翻译密钥和应用权限。")
    return token


def _translate(text, token, source_language, target_language):
    response = requests.post(
        "https://aip.baidubce.com/rpc/2.0/mt/texttrans/v1",
        params={"access_token": token},
        json={"q": text, "from": source_language, "to": target_language},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    translations = payload.get("result", {}).get("trans_result", [])
    if not translations or not translations[0].get("dst"):
        raise RuntimeError("机器翻译失败：" + json.dumps(payload, ensure_ascii=False))
    return translations[0]["dst"].strip()


def _english_chunks(text, max_chars=900):
    text = " ".join(text.split())
    chunks = []
    current = ""
    for sentence in text.replace("?", "?\n").replace("!", "!\n").replace(".", ".\n").splitlines():
        sentence = sentence.strip()
        if not sentence:
            continue
        while len(sentence) > max_chars:
            split_at = sentence.rfind(" ", 0, max_chars + 1)
            if split_at <= 0:
                split_at = max_chars
            part, sentence = sentence[:split_at].strip(), sentence[split_at:].strip()
            if part:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(part)
        if current and len(current) + len(sentence) + 1 > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


def _speech_token(api_key, secret_key):
    response = requests.get(
        BAIDU_TOKEN_URL,
        params={"grant_type": "client_credentials", "client_id": api_key, "client_secret": secret_key},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("无法获取语音合成令牌，请检查语音密钥和应用权限。")
    return token


def _synthesize(text, token, destination, language):
    response = requests.post(
        BAIDU_TTS_URL,
        data={
            "tex": text,
            "tok": token,
            "cuid": "VideoToSrt",
            "ctp": 1,
            "lan": language,
            "spd": 5,
            "pit": 5,
            "vol": 5,
            "per": 0,
        },
        timeout=60,
    )
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "")
    if "audio" not in content_type.lower():
        try:
            payload = response.json()
            detail = payload.get("err_msg") or payload.get("err_detail") or json.dumps(payload, ensure_ascii=False)
        except ValueError:
            detail = response.text[:500]
        raise RuntimeError("百度语音合成失败：" + detail)
    destination.write_bytes(response.content)


def _probe_duration(path):
    ffprobe = Path(_ffmpeg_path()).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    result = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "无法读取合成音频时长。")
    return float(result.stdout.strip())


def _tempo_filter(source_duration, target_duration):
    factor = source_duration / max(target_duration, 0.1)
    filters = []
    while factor > 2:
        filters.append(2.0)
        factor /= 2
    while factor < 0.5:
        filters.append(0.5)
        factor /= 0.5
    filters.append(factor)
    return ",".join(f"atempo={speed:.5f}" for speed in filters)


def translate_media_to_english_mp3(
    media_path,
    output_path,
    speech_keys,
    translation_keys,
    progress=None,
    source_language="zh",
    target_language="en",
):
    """Translate speech and synthesize it in the selected target language."""
    media_path = Path(media_path).resolve()
    output_path = Path(output_path).resolve()
    api_key, secret_key = speech_keys
    translation_ak, translation_sk = translation_keys

    def report(value, message):
        if progress:
            progress(value, message)

    if not media_path.is_file():
        raise FileNotFoundError(f"找不到媒体文件：{media_path}")

    with tempfile.TemporaryDirectory(prefix="video_to_srt_audio_") as temp_dir:
        temp = Path(temp_dir)
        wav_path = temp / "source.wav"
        report(2, "正在提取并转换音频…")
        _run_ffmpeg(["-y", "-i", str(media_path), "-vn", "-ac", "1", "-ar", "16000", str(wav_path)])

        regions = find_speech_regions(str(wav_path))
        if not regions:
            raise RuntimeError("没有检测到可识别的语音。")

        source_label = LANGUAGE_LABELS.get(source_language, source_language)
        target_label = LANGUAGE_LABELS.get(target_language, target_language)
        dev_pid = 1537 if source_language == "zh" else 1737
        recognizer = SpeechRecognizer(api_key, secret_key, 16000, "wav", dev_pid)
        if not recognizer.token or recognizer.token in (0, 1):
            raise RuntimeError("百度语音识别鉴权失败，请检查语音密钥及应用权限。")

        transcripts = []
        for index, region in enumerate(regions):
            start, end, _number = region
            segment = temp / f"speech_{index:04d}.wav"
            _run_ffmpeg([
                "-y", "-ss", str(max(0, start - 0.2)), "-t", str(end - start + 0.4),
                "-i", str(wav_path), "-ac", "1", "-ar", "16000", str(segment),
            ])
            transcript = recognizer(str(segment))
            if transcript == "Conversion failed" or isinstance(transcript, int):
                raise RuntimeError(f"第 {index + 1} 段语音识别失败，请检查网络、语音额度和应用权限。")
            if transcript:
                transcripts.append((region, transcript.strip()))
            report(5 + int((index + 1) / len(regions) * 35), f"正在识别{source_label}语音…（{index + 1}/{len(regions)}）")
        if not transcripts:
            raise RuntimeError(f"语音识别没有返回{source_label}文本，请确认音频清晰且包含对应语言。")

        report(42, "正在获取机器翻译服务…")
        translation_access_token = _translation_token(translation_ak, translation_sk)
        translated_regions = []
        for index, (region, transcript) in enumerate(transcripts):
            translated_regions.append((region[0], region[1], _translate(
                transcript, translation_access_token, source_language, target_language
            )))
            report(42 + int((index + 1) / len(transcripts) * 25), f"正在翻译为{target_label}…（{index + 1}/{len(transcripts)}）")

        report(68, "正在连接百度语音合成…")
        tts_token = _speech_token(api_key, secret_key)
        if not translated_regions:
            raise RuntimeError("翻译结果为空，无法生成语音。")

        audio_parts = []
        previous_end = None
        for index, (start, end, paragraph) in enumerate(translated_regions):
            chunks = _english_chunks(paragraph)
            if not chunks:
                raise RuntimeError(f"第 {index + 1} 段翻译结果为空，无法合成语音。")
            chunk_parts = []
            for chunk_index, chunk in enumerate(chunks):
                part = temp / f"speech_{index:04d}_{chunk_index:03d}.mp3"
                _synthesize(chunk, tts_token, part, target_language)
                chunk_parts.append(part)

            region_audio = chunk_parts[0]
            if len(chunk_parts) > 1:
                chunk_list = temp / f"speech_{index:04d}_parts.txt"
                chunk_list.write_text(
                    "".join(f"file '{part.as_posix()}'\n" for part in chunk_parts), encoding="utf-8"
                )
                region_audio = temp / f"speech_{index:04d}_joined.mp3"
                _run_ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", str(chunk_list), "-c:a", "libmp3lame", "-q:a", "4", str(region_audio)])

            target_duration = max(0.2, end - start)
            fitted_audio = temp / f"speech_{index:04d}_fitted.mp3"
            tempo = _tempo_filter(_probe_duration(region_audio), target_duration)
            _run_ffmpeg(["-y", "-i", str(region_audio), "-filter:a", tempo, "-vn", "-c:a", "libmp3lame", "-q:a", "4", str(fitted_audio)])

            if previous_end is not None and start > previous_end:
                silence_duration = start - previous_end
                silence = temp / f"silence_{index:04d}.mp3"
                _run_ffmpeg(["-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", str(silence_duration), "-c:a", "libmp3lame", "-q:a", "7", str(silence)])
                audio_parts.append(silence)
            audio_parts.append(fitted_audio)
            previous_end = end
            report(68 + int((index + 1) / len(translated_regions) * 27), f"正在合成并匹配语速…（{index + 1}/{len(translated_regions)}）")

        concat_file = temp / "parts.txt"
        concat_file.write_text(
            "".join(f"file '{part.as_posix()}'\n" for part in audio_parts), encoding="utf-8"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        report(97, "正在合并 MP3 文件…")
        _run_ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c:a", "libmp3lame", "-q:a", "4", str(output_path)])

    report(100, f"{target_label} MP3 已生成。")
    return str(output_path)
