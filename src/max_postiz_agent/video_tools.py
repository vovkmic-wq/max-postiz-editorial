# Проверка качества видео, сравнение кадров и сборка монтажа.
"""Bounded, cross-platform video QA, visual fingerprints and montage."""

import subprocess

import imageio_ffmpeg

from .config import AgentError, inside
from .media import inspect_video


# Запускаем обработку медиа с ограниченным временем выполнения.
def ffmpeg(args, timeout=120):
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        result = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", *args],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    # Обрабатываем (OSError, subprocess.TimeoutExpired) и выбираем допустимый исход.
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentError("VIDEO_PROCESS_TIMEOUT") from exc
    # Обрабатываем ветвь при условии: result.returncode.
    if result.returncode:
        raise AgentError("VIDEO_PROCESS_FAILED")
    return result


# Извлекаем признаки кадров для поиска визуальных повторов.
def frame_fingerprints(path, duration):
    hashes = []
    # Последовательно обрабатываем элементы: (0.1, 0.5, 0.9).
    for fraction in (0.1, 0.5, 0.9):
        result = ffmpeg(
            [
                "-ss",
                str(duration * fraction),
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                "scale=9:8,format=rgb24",
                "-f",
                "rawvideo",
                "pipe:1",
            ],
            30,
        )
        frame = result.stdout
        # Обрабатываем ветвь при условии: not frame.
        if not frame:
            # A short low-fps clip can end before the rounded duration's sample point.
            # Scale before reversing so buffering the final frame stays small.
            frame = ffmpeg(
                ["-i", str(path), "-vf", "scale=9:8,format=rgb24,reverse",
                 "-frames:v", "1", "-f", "rawvideo", "pipe:1"],
                30,
            ).stdout
        # Обрабатываем ветвь при условии: len(frame) != 216.
        if len(frame) != 216:
            raise AgentError("VIDEO_FRAME_INVALID")
        gray = [sum(frame[i : i + 3]) // 3 for i in range(0, 216, 3)]
        bits = 0
        # Последовательно обрабатываем элементы: range(8).
        for y in range(8):
            # Последовательно обрабатываем элементы: range(8).
            for x in range(8):
                bits = (bits << 1) | int(gray[y * 9 + x] > gray[y * 9 + x + 1])
        rgb = [sum(frame[channel::3]) // 72 for channel in range(3)]
        hashes.append(f"{bits:016x}" + "".join(f"{value:02x}" for value in rgb))
    return hashes


# Сопоставляем признаки изображений или кадров.
def visually_similar(left, right, max_distance=6):
    return len(left) == len(right) == 3 and all(
        (int(a[:16], 16) ^ int(b[:16], 16)).bit_count() <= max_distance
        and all(abs(int(a[c : c + 2], 16) - int(b[c : c + 2], 16)) <= 25 for c in (16, 18, 20))
        for a, b in zip(left, right, strict=True)
    )


# Проверяем техническое качество перед допуском к публикации.
def quality_check(root, relative, options=None):
    options = options or {}
    info = inspect_video(
        root, relative, options.get("max_duration_seconds", 60), options.get("max_bytes", 104857600)
    )
    path = inside(root, relative)
    # Decode the complete clip: metadata alone does not prove decodable video frames.
    decoded = ffmpeg(
        [
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-vf",
            "blackdetect=d=0.5:pix_th=0.1:pic_th=0.98,freezedetect=n=-50dB:d=2",
            "-an",
            "-f",
            "null",
            "-",
        ],
        120,
    )
    log = decoded.stderr.decode("utf-8", errors="replace")
    flags = []
    # Обрабатываем ветвь при условии: 'black_start:' in log.
    if "black_start:" in log:
        flags.append("BLACK_FRAMES_REVIEW")
    # Обрабатываем ветвь при условии: 'freeze_start' in log.
    if "freeze_start" in log:
        flags.append("FROZEN_SCENE_REVIEW")
    streams = ffmpeg(["-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-f", "null", "-"])
    output = streams.stderr.decode("utf-8", errors="replace")
    has_audio = "Audio:" in output
    # Обрабатываем ветвь при условии: options.get('require_audio') and (not has_audio).
    if options.get("require_audio") and not has_audio:
        flags.append("MISSING_AUDIO_REVIEW")
    return {
        **info,
        "frames": frame_fingerprints(path, info["duration_seconds"]),
        "flags": flags,
        "has_audio": has_audio,
        "decoded": True,
    }


# Создаём монтаж из проверенных локальных сцен.
def render_montage(root, scene_paths, durations, output_relative, options=None):
    options = options or {}
    # Обрабатываем ветвь при условии: not scene_paths or len(scene_paths) != len(durations) or len(scene_paths) > 12.
    if not scene_paths or len(scene_paths) != len(durations) or len(scene_paths) > 12:
        raise AgentError("SCENE_LIST_INVALID")
    width, height = options.get("width", 720), options.get("height", 1280)
    # Обрабатываем ветвь при условии: width not in {160, 360, 720, 1080} or height not in {288, 640, 1280, 1920}.
    if width not in {160, 360, 720, 1080} or height not in {288, 640, 1280, 1920}:
        raise AgentError("RENDER_SIZE_INVALID")
    # Обрабатываем ветвь при условии: any((not isinstance(d, (int, float)) or isinstance(d, bool) or (not 0 < d <= 60) for d in durations)) or su….
    if any(
        not isinstance(d, (int, float)) or isinstance(d, bool) or not 0 < d <= 60 for d in durations
    ) or sum(durations) > options.get("max_duration_seconds", 60):
        raise AgentError("SCENE_DURATION_INVALID")
    target = inside(root, output_relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Unique work paths; FFmpeg paths are argv entries, never interpolated into filter text.
    work = target.parent / (target.stem + "-parts")
    work.mkdir(exist_ok=True)
    parts = []
    # Последовательно обрабатываем элементы: enumerate(zip(scene_paths, durations, strict=True)).
    for index, (relative, duration) in enumerate(zip(scene_paths, durations, strict=True)):
        path = inside(root, relative)
        info = inspect_video(root, relative)
        # Обрабатываем ветвь при условии: info['duration_seconds'] + 0.1 < duration.
        if info["duration_seconds"] + 0.1 < duration:
            raise AgentError("SCENE_TOO_SHORT")
        part = work / f"{index}.mp4"
        audio = (
            []
            if info["has_audio"]
            else ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        )
        ffmpeg(
            [
                "-y",
                "-i",
                str(path),
                *audio,
                "-t",
                str(duration),
                "-vf",
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24",
                "-map",
                "0:v:0",
                "-map",
                "0:a:0" if info["has_audio"] else "1:a:0",
                "-af",
                "aresample=48000",
                "-ac",
                "2",
                "-c:a",
                "aac",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-pix_fmt",
                "yuv420p",
                str(part),
            ]
        )
        parts.append(part)
    args = ["-y"]
    # Последовательно обрабатываем элементы: parts.
    for part in parts:
        args += ["-i", str(part)]
    labels = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(len(parts)))
    args += [
        "-filter_complex",
        labels + f"concat=n={len(parts)}:v=1:a=1[v][a]",
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:a",
        "aac",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(target),
    ]
    ffmpeg(args)
    return quality_check(root, output_relative, options)
