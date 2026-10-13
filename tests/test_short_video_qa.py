import subprocess

import imageio_ffmpeg

from max_postiz_agent.video_tools import quality_check


# Проверяем сценарий: short eight fps clip samples last decoded frame.
def test_short_eight_fps_clip_samples_last_decoded_frame(tmp_path):
    path = tmp_path / "short.mp4"
    subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-f", "lavfi",
        "-i", "testsrc2=size=192x192:rate=8", "-frames:v", "9",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
    ], check=True)
    qa = quality_check(tmp_path, "short.mp4")
    assert qa["decoded"]
    assert len(qa["frames"]) == 3
    assert qa["duration_seconds"] < 1.2
