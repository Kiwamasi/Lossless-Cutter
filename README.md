# Video Cutter

Quick, lossless video cutter. Python + PySide6 (Qt) UI on top of `ffmpeg -c copy`,
so a 56 GB / 14 h recording splits in about a minute with no quality loss.

## Requirements

The app does **not** install anything itself. You need three things:

| Requirement | Why | How to install |
|---|---|---|
| **Python 3.10+** | Runs the app | `winget install Python.Python.3.13`, or download from [python.org](https://www.python.org/downloads/). In the installer, tick **"Add python.exe to PATH"**. |
| **PySide6** | The Qt UI and video player | `pip install -r requirements.txt` (run in this folder) or `pip install PySide6` |
| **ffmpeg + ffprobe** | Does the actual cutting and reads video info | `winget install Gyan.FFmpeg` (installs both) |

After installing, **open a new terminal** so the updated PATH is picked up, then check everything with:

    python --version
    python -c "import PySide6; print(PySide6.__version__)"
    ffmpeg -version
    ffprobe -version

If any of these print an error, that requirement is missing or not on PATH.

> If PySide6 is missing, `Video Cutter.bat` fails silently (it starts Python without a console window).
> Run `python main.py` in a terminal to see the error. If ffmpeg is missing, the app tells you on startup.

## Run

    "Video Cutter.bat"            (or: python main.py [video])

## Use

1. Open or drag a video onto the window.
2. Move the playhead (click/drag the timeline, arrow keys) and press **S** to split.
   The first split cuts the whole video in two; further splits cut the part under the playhead.
   Or use **Split into N…** for equal parts, or **I** / **O** to mark individual clips.
3. Drag part edges on the timeline or double-click times/names in the table to adjust.
4. **Export**. Each part is written next to the source (or to the chosen folder).

Made a mistake? **Ctrl+Z** undoes any change to the parts (split, delete, drag, rename, in point, clear all),
and **Ctrl+Y** / **Ctrl+Shift+Z** redoes it. Press **F1** for all keyboard shortcuts.

## Notes

- The player's volume slider only affects preview playback. Exported files keep the original audio untouched.
- Stream copy can only start a part on a keyframe. With *Snap cuts to keyframes* on (default),
  every cut point moves to the nearest keyframe, so parts start exactly where shown and don't overlap.
- Parts are split by time. The size column is an estimate based on the average bitrate.
- Only video and audio tracks are copied. Subtitle and chapter tracks in the source are dropped.
