# Video Cutter

Windows desktop app: a fast, lossless video cutter growing into a simple editor. Python + PySide6 (Qt) UI;
all cutting is done by `ffmpeg -c copy` (never re-encode — speed on huge files, e.g. 56 GB / 14 h, is the point).
User-facing docs are in README.md; keep it updated when features change.

## Run

    python app\main.py [video ...]

Requirements: Python 3.10+, PySide6, ffmpeg + ffprobe on PATH. No other dependencies (numpy is deliberately
not used — keep it that way unless the user agrees to add a dependency).

## Layout

- `app/main.py` — the whole app, in sections: helpers (ffprobe/keyframe lookups), `Segment`, `Clip`, waveform
  (`Waveform`, `WaveformLoader`), `Cuts`, `Timeline` widget, `Exporter`, `MainWindow`.
- `tools/make_icon.py` draws `app/icon.ico`/`icon.png`; `tools/create_shortcut.ps1` makes the Windows shortcut.

## Key concepts

- **Clips.** Several video files (`MainWindow.clips`, each a `Clip`) play back to back. *Source time* is time
  on that combined timeline; `_offsets[i]` is where clip i starts. Only code touching real files maps it back:
  `_clip_at(t)` -> (clip, time in file), `_file_pieces(a, b)` for export, the player (`_player_seek`, which
  switches files and applies the position once the new file has loaded), keyframe lookups (`snap`,
  `_clean_restart`) and the waveform (one `WaveformLoader` per file, stitched together by `_extend_waveform`).
  Reordering or removing clips clears parts, cuts and undo; appending keeps them.
- **Source time vs edit time.** All stored times (segments, in point, silences, player position) are in
  *source* seconds. `Cuts` holds deleted source ranges; *edit time* is source time with those removed.
  Everything the user sees (timeline, ruler, clock, parts table, Go to time) is edit time — convert with
  `cuts.to_edit()` / `cuts.to_source()` at UI boundaries. `cuts.span(a, b)` is a range's edited length;
  `cuts.kept(a, b)` gives the surviving source pieces. `Cuts` is immutable; set it via `MainWindow._set_cuts`.
- **Keyframes.** Stream copy can only start a piece on a keyframe. With "Snap cuts to keyframes" on, cut points
  snap (`snap()`), and deleting a silent block ends the cut on the last keyframe inside it (`_clean_restart`).
  The start of every file also counts as a clean cut point.
- **Parts** (`Segment`s) are what gets exported, one file each. With no parts but some cuts, or several
  clips, export writes the whole timeline as one file. Multi-piece parts are joined in one pass with the ffmpeg concat demuxer
  (inpoint/outpoint, temp list file); single-piece parts use plain `-ss/-t`.
- **Waveform.** `WaveformLoader` streams 8 kHz mono s16 from ffmpeg via QProcess and stores one peak byte
  (dB-scaled) per 10 ms, plus coarser levels for fast drawing. Silence detection runs on those bytes
  (`Waveform.silences`), so changing its settings never re-reads the file.
- **Undo/redo** works on `_snapshot()` (parts, in point, cuts, selection). Call `_push_undo()` before any change.

## Conventions

- Match the existing style: compact, few comments, statusBar messages for user feedback,
  QSettings (`"VideoCutter", "VideoCutter"`) for remembered options.
- Buttons don't take keyboard focus (`_button` sets NoFocus); input widgets use ClickFocus so the
  single-key shortcuts (Space, arrows, S, I, O, Delete…) keep working.
- `app/main.py` has CRLF line endings.

## Testing

No test suite. Verify changes by driving `MainWindow` from a small script (create a `QApplication`, load a
test video made with ffmpeg's `lavfi` sources, call methods, `win.grab().save(...)` for a screenshot),
and check exports with ffprobe. Put scripts and test media in a scratch/temp folder, not the repo.
Run with `PYTHONIOENCODING=utf-8` (UI strings contain ✔ etc.), and clean up any QSettings keys a test sets.
