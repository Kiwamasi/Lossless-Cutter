<img src="app/icon.png" width="96" align="right" alt="">

# Video Cutter

Quick, lossless video cutter. Python + PySide6 (Qt) UI on top of `ffmpeg -c copy`,
so a 56 GB / 14 h recording splits in about a minute with no quality loss.

## Requirements

The app does **not** install anything itself. You need three things:

| Requirement | Why | How to install |
|---|---|---|
| **Python 3.10+** | Runs the app | `winget install Python.Python.3.13`, or download from [python.org](https://www.python.org/downloads/). In the installer, tick **"Add python.exe to PATH"**. |
| **PySide6** | The Qt UI and video player | `pip install PySide6` |
| **ffmpeg + ffprobe** | Does the actual cutting and reads video info | `winget install Gyan.FFmpeg` (installs both) |

After installing, **open a new terminal** so the updated PATH is picked up, then check everything with:

    python --version
    python -c "import PySide6; print(PySide6.__version__)"
    ffmpeg -version
    ffprobe -version

If any of these print an error, that requirement is missing or not on PATH.

> If PySide6 is missing, the shortcut fails silently (it starts Python without a console window).
> Run `python app\main.py` in a terminal to see the error. If ffmpeg is missing, the app tells you on startup.

## Run

Double-click **Video Cutter** (the shortcut with the icon) in this folder. It starts the app with no console window.

The shortcut isn't in the repository (it contains paths specific to one PC). To create it, for example after
cloning, moving the folder or reinstalling Python, run in this folder:

    powershell -ExecutionPolicy Bypass -File tools\create_shortcut.ps1              # shortcut in this folder
    powershell -ExecutionPolicy Bypass -File tools\create_shortcut.ps1 -Desktop     # ...and on the desktop
    powershell -ExecutionPolicy Bypass -File tools\create_shortcut.ps1 -StartMenu   # ...and in the Start menu

You can also right-click the shortcut → *Pin to Start* / *Show more options → Pin to taskbar*.
From a terminal: `python app\main.py [video]`.

## Project layout

    app/        main.py (the whole app), icon.ico (app + shortcut icon), icon.png
    tools/      make_icon.py (draws the icon), create_shortcut.ps1 (makes the shortcut)

The icon is drawn in code. Edit `tools/make_icon.py` and run `python tools\make_icon.py` to regenerate it.

## Use

1. Open or drag one or more videos onto the window. Several videos play back to back on one timeline;
   the **Videos** tab lists them, and **Sort by date created** (or ▲ / ▼) sets their order.
   Changing the order clears your parts and deleted blocks, so set it first.
2. Move the playhead (click/drag the timeline, arrow keys) and press **S** to split.
   The first split cuts the whole video in two; further splits cut the part under the playhead.
   Or use **Split into N…** for equal parts, or **I** / **O** to mark individual clips.
   The timeline shows the audio waveform. **Highlight silence** marks quiet stretches in red;
   *At least* sets how long a quiet stretch must last to count, *Quieter than* how quiet it must be.
   Click a red block to select it (Ctrl+click for several, Ctrl+A for all) and press **Delete** to cut it out:
   the video and audio either side join up. With no parts, **Export edited video** writes the whole shortened video.
3. Drag part edges on the timeline or double-click times/names in the table to adjust.
4. **Export**. Each part is written next to the source (or to the chosen folder).

Made a mistake? **Ctrl+Z** undoes any change to the parts (split, delete, drag, rename, in point, clear all),
and **Ctrl+Y** / **Ctrl+Shift+Z** redoes it. Press **F1** for all keyboard shortcuts.

## Notes

- The player's volume slider only affects preview playback. Exported files keep the original audio untouched.
- Stream copy can only start a part on a keyframe. With *Snap cuts to keyframes* on (default),
  every cut point moves to the nearest keyframe, so parts start exactly where shown and don't overlap.
- Parts are split by time. The size column is an estimate based on the average bitrate.
- When a silent block is deleted (with snapping on) the cut ends on the last keyframe inside the block,
  so a sliver of silence may stay but no sound is lost. A block with no keyframe inside can't be cut cleanly.
- Joining several videos without re-encoding only works if they share the same format (codec, size, frame rate),
  e.g. recordings from the same camera or OBS setup. The Videos tab warns when they don't.
- Only video and audio tracks are copied. Subtitle and chapter tracks in the source are dropped.
