"""Video Cutter - a quick, lossless video cutter.

Plays the video with Qt, lets you mark parts on a timeline, and exports each
part with `ffmpeg -c copy` (no re-encode), so even huge files cut in seconds.
"""
import bisect
import datetime
import itertools
import json
import tempfile
import math
import os
import re
import shutil
import subprocess
import sys
from array import array
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QLineF, QObject, QPointF, QProcess, QRectF, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QKeySequence, QPainter, QPalette, QPen, QPolygonF
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDoubleSpinBox, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton, QSlider, QSpinBox, QSplitter, QStyle, QTableWidget, QTabWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

APP_NAME = "Video Cutter"
VIDEO_EXTS = "*.mp4 *.mkv *.mov *.m4v *.ts *.mts *.flv *.avi *.webm"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SEG_COLORS = [
    QColor(70, 130, 220), QColor(225, 140, 50), QColor(80, 180, 110),
    QColor(190, 90, 200), QColor(210, 80, 90), QColor(60, 180, 190),
]

SHORTCUTS_HELP = """\
Space\tPlay / pause
← / →\tBack / forward 5 seconds
Shift + ← / →\tBack / forward 1 minute
PgUp / PgDn\tBack / forward 10 minutes
, / .\tStep one frame back / forward
Home / End\tJump to start / end
[ / ]\tJump to previous / next cut point
Ctrl+G\tGo to a time

S\tSplit the part under the playhead (first press splits the whole video)
N\tSplit the whole video into N equal parts
I / O\tMark the in / out point of a new part
Delete\tRemove the selected part, or the selected silent blocks
Esc\tClear the in point / selection
Ctrl+Z\tUndo the last change to the parts
Ctrl+Y / Ctrl+Shift+Z\tRedo

Click a red block\tSelect it (Ctrl+click to select several)
Ctrl+A\tSelect every silent block

Mouse wheel on timeline\tZoom
Shift + wheel\tScroll the timeline
Drag a part's edge\tAdjust it
Ctrl+0 / Ctrl+= / Ctrl+-\tZoom to fit / in / out

Ctrl+O\tOpen one or more videos
Ctrl+Shift+O\tAdd videos to the end of the timeline
Ctrl+E\tExport parts"""


# ---------------------------------------------------------------- helpers

def fmt_time(t, ms=True):
    total_ms = int(round(max(0.0, t) * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, msr = divmod(rem, 1000)
    return f"{h}:{m:02}:{s:02}.{msr:03}" if ms else f"{h}:{m:02}:{s:02}"


def parse_time(text):
    """Accepts '1:02:03.5', '62:03', or '3723.5' (seconds)."""
    parts = text.strip().split(":")
    if not parts[0] or len(parts) > 3:
        return None
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if any(n < 0 for n in nums):
        return None
    t = 0.0
    for n in nums:
        t = t * 60 + n
    return t


def fmt_size(b):
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.0f} {unit}" if unit == "B" else f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def sanitize_filename(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".") or "part"


def probe(ffprobe, path):
    res = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "format=duration,size:stream=codec_type,codec_name,avg_frame_rate,width,height", "-of", "json", path],
        capture_output=True, text=True, creationflags=NO_WINDOW, timeout=120,
    )
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "ffprobe failed")
    data = json.loads(res.stdout)
    fmt = data.get("format", {})
    info = {
        "duration": float(fmt.get("duration") or 0),
        "size": int(fmt.get("size") or os.path.getsize(path)),
        "fps": 30.0, "width": 0, "height": 0, "vcodec": "", "acodec": "",
    }
    st = os.stat(path)
    info["created"] = getattr(st, "st_birthtime", st.st_ctime)  # file creation time on Windows
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not info["vcodec"]:
            num, _, den = s.get("avg_frame_rate", "0/1").partition("/")
            try:
                if float(num) > 0 and float(den or 1) > 0:
                    info["fps"] = float(num) / float(den or 1)
            except ValueError:
                pass
            info["width"], info["height"] = s.get("width", 0), s.get("height", 0)
            info["vcodec"] = s.get("codec_name", "?")
        elif s.get("codec_type") == "audio" and not info["acodec"]:
            info["acodec"] = s.get("codec_name", "?")
    return info


def keyframes(ffprobe, path, t0, t1):
    """Times of the video keyframes around t0..t1 (roughly; may include some just outside).

    Only reads packet headers in that window, so it is fast even on huge files.
    """
    res = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-read_intervals", f"{max(0.0, t0):.3f}%{t1:.3f}",
         "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", path],
        capture_output=True, text=True, creationflags=NO_WINDOW, timeout=60,
    )
    keys = []
    for line in res.stdout.splitlines():
        pts, _, flags = line.partition(",")
        if "K" in flags:
            try:
                keys.append(float(pts))
            except ValueError:
                pass
    return keys


def nearest_keyframe(ffprobe, path, t):
    """Time of the video keyframe closest to t, or None if none was found."""
    for window in (10, 60):
        keys = keyframes(ffprobe, path, t - window, t + window)
        if keys:
            return min(keys, key=lambda k: abs(k - t))
    return None


def last_keyframe_in(ffprobe, path, t0, t1):
    """The last video keyframe after t0 and at or before t1, or None."""
    return max((k for k in keyframes(ffprobe, path, t0, t1) if t0 < k <= t1 + 1e-3), default=None)


@dataclass(eq=False)
class Segment:
    start: float
    end: float
    name: str | None = None  # None = use the default "<file> - partN" name

    @property
    def length(self):
        return self.end - self.start


@dataclass(eq=False)
class Clip:
    """One video file on the timeline. The files play back to back in list order."""
    path: str
    info: dict

    @property
    def name(self):
        return Path(self.path).name

    @property
    def duration(self):
        return self.info["duration"]

    @property
    def format(self):
        i = self.info
        video = f"{i['vcodec']} {i['width']}×{i['height']} {i['fps']:.3g} fps" if i["vcodec"] else "no video"
        return f"{video} · {i['acodec'] or 'no audio'}"


# ---------------------------------------------------------------- waveform

DB_FLOOR = -96.0       # quietest level a 16-bit sample can hold
WAVE_DISPLAY_DB = -60  # the waveform's drawn height spans this many dB up to full scale


def db_to_byte(db):
    return max(0, min(255, round((db - DB_FLOOR) / -DB_FLOOR * 255)))


def byte_to_db(b):
    return DB_FLOOR + b / 255 * -DB_FLOOR


# Peak sample value (0..32768) -> level byte, and level byte -> drawn height (0..1).
PEAK_TO_BYTE = bytes([0] + [db_to_byte(20 * math.log10(m / 32768)) for m in range(1, 32769)])
BYTE_TO_HEIGHT = [max(0.0, 1 - byte_to_db(b) / WAVE_DISPLAY_DB) for b in range(256)]


class Waveform:
    """Audio peak levels, one byte (a dB value, see db_to_byte) per 1/RATE seconds.

    Coarser copies (each FACTOR times shorter) keep drawing fast when zoomed out on long videos.
    """
    RATE = 100
    FACTOR = 16
    LEVELS = 4

    def __init__(self):
        self.levels = [bytearray() for _ in range(self.LEVELS)]

    @property
    def loaded(self):
        """Seconds of audio read so far."""
        return len(self.levels[0]) / self.RATE

    def append(self, peaks):
        self.levels[0] += peaks
        f = self.FACTOR
        for lo, hi in zip(self.levels, self.levels[1:]):
            for j in range(len(hi), len(lo) // f):
                hi.append(max(lo[j * f:(j + 1) * f]))

    def column_peaks(self, edges, dt):
        """The loudest level between each pair of neighbouring times in edges (stops where data ends).

        dt is the shortest column, in seconds; it picks how coarse a copy to read.
        """
        i = 0
        while i + 1 < self.LEVELS and self.FACTOR ** (i + 1) / self.RATE <= dt:
            i += 1
        data, rate = self.levels[i], self.RATE / self.FACTOR ** i
        out = []
        for t0, t1 in zip(edges, edges[1:]):
            a = int(t0 * rate)
            if a >= len(data):
                break
            out.append(max(data[a:max(a + 1, int(t1 * rate))]))
        return out

    def silences(self, threshold_db, min_len):
        """(start, end) of every stretch at least min_len seconds long that stays below threshold_db."""
        limit = db_to_byte(threshold_db)
        quiet = self.levels[0].translate(bytes(1 if b < limit else 0 for b in range(256)))
        n = max(1, math.ceil(min_len * self.RATE - 1e-9))
        # The lookbehind only lets a match start at the beginning of a quiet run, keeping this linear.
        return [(m.start() / self.RATE, m.end() / self.RATE)
                for m in re.finditer(rb"(?<!\x01)\x01{%d,}" % n, quiet)]


class WaveformLoader(QObject):
    """Decodes the first audio track with ffmpeg in the background, filling a Waveform as it goes."""
    updated = Signal()
    done = Signal(bool, str)

    SAMPLE_RATE = 8000

    def __init__(self, ffmpeg, src, parent=None):
        super().__init__(parent)
        self.wave = Waveform()
        self.cancelled = False
        self.complete = False
        self._spb = self.SAMPLE_RATE // Waveform.RATE  # samples per peak
        self._buf = bytearray()
        self.proc = QProcess(self)
        self.proc.setProgram(ffmpeg)
        self.proc.setArguments([
            "-hide_banner", "-nostdin", "-loglevel", "error",
            "-i", src, "-map", "0:a:0", "-ac", "1", "-ar", str(self.SAMPLE_RATE), "-f", "s16le", "pipe:1",
        ])
        self.proc.readyReadStandardOutput.connect(self._on_output)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)

    def start(self):
        self.proc.start()

    def cancel(self):
        self.cancelled = True
        if self.proc.state() != QProcess.NotRunning:
            self.proc.kill()
            self.proc.waitForFinished(2000)

    def _consume(self, final=False):
        step = self._spb * 2
        n = len(self._buf) if final else len(self._buf) // step * step
        n -= n % 2
        if not n:
            return
        samples = array("h")
        samples.frombytes(bytes(self._buf[:n]))
        del self._buf[:n]
        spb = self._spb
        self.wave.append(bytes(PEAK_TO_BYTE[max(max(c), -min(c))]
                               for c in (samples[i:i + spb] for i in range(0, len(samples), spb))))

    def _on_output(self):
        self._buf += self.proc.readAllStandardOutput().data()
        self._consume()
        self.updated.emit()

    def _on_error(self, err):
        if err == QProcess.FailedToStart and not self.cancelled:
            self.complete = True
            self.done.emit(False, "Could not start ffmpeg.")

    def _on_finished(self, code, _status):
        if self.cancelled:
            return
        self._buf += self.proc.readAllStandardOutput().data()
        self._consume(final=True)
        self.complete = True
        if code != 0 and not self.wave.levels[0]:
            err = self.proc.readAllStandardError().data().decode(errors="ignore")
            self.done.emit(False, "No audio track" if "matches no streams" in err else "Couldn't read the audio")
        else:
            self.done.emit(True, "")


# ---------------------------------------------------------------- cuts

class Cuts:
    """Ranges of the source (in source seconds) that were deleted; everything else plays back to back.

    "Edit time" is the time on the shortened video, i.e. source time minus everything removed before it.
    Immutable: add() returns a new Cuts.
    """

    def __init__(self, ranges=()):
        merged = []
        for a, b in sorted(ranges):
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            elif b > a:
                merged.append((a, b))
        self.ranges = tuple(merged)
        self._starts = [a for a, _ in merged]
        self._before = list(itertools.accumulate((b - a for a, b in merged), initial=0.0))
        self._edit_starts = [a - self._before[i] for i, (a, _) in enumerate(merged)]

    def __bool__(self):
        return bool(self.ranges)

    def add(self, ranges):
        return Cuts(self.ranges + tuple(ranges))

    def containing(self, t):
        i = bisect.bisect_right(self._starts, t) - 1
        return self.ranges[i] if i >= 0 and t < self.ranges[i][1] else None

    def skip(self, t):
        """t, or the end of the deleted range it falls in."""
        r = self.containing(t)
        return r[1] if r else t

    def to_edit(self, t):
        i = bisect.bisect_right(self._starts, t) - 1
        if i < 0:
            return t
        a, b = self.ranges[i]
        return a - self._before[i] if t < b else t - self._before[i + 1]

    def to_source(self, e):
        i = bisect.bisect_right(self._edit_starts, e) - 1
        return e if i < 0 else e + self._before[i + 1]

    def span(self, a, b):
        """Length of source range a..b once the deleted bits are taken out."""
        return self.to_edit(b) - self.to_edit(a)

    def kept(self, a, b):
        """The pieces of source range a..b that were not deleted."""
        out, cur = [], a
        for ra, rb in self.ranges:
            if rb <= cur:
                continue
            if ra >= b:
                break
            if ra > cur:
                out.append((cur, ra))
            cur = max(cur, rb)
        if cur < b:
            out.append((cur, b))
        return [(x, y) for x, y in out if y - x > 1e-6]


# ---------------------------------------------------------------- timeline

class Timeline(QWidget):
    """Draws and edits everything in edit time; all times it takes and emits are source times."""
    seekRequested = Signal(float)
    selectionChanged = Signal(object)          # Segment or None
    blocksChanged = Signal()                   # selected_blocks changed
    edgeDragStarted = Signal()
    edgeDragged = Signal()
    edgeDragFinished = Signal(object, str)     # Segment, "start" | "end"

    MARGIN = 10
    RULER_H = 24
    CLIPS_H = 16
    PARTS_H = 46
    EDGE_PX = 6
    TICK_STEPS = [0.1, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.duration = 0.0                       # of the source
        self.clips: list[tuple[float, float, str]] = []  # (start, end, file name) of each video
        self.cuts = Cuts()
        self.waveform: Waveform | None = None
        self.wave_status = ""                     # e.g. "Reading audio… 40%", shown in the waveform lane
        self.silences: list[tuple[float, float]] = []
        self.selected_blocks: list[tuple[float, float]] = []  # silences picked for deletion
        self.segments: list[Segment] = []
        self.selected: Segment | None = None
        self.position = 0.0
        self.in_point = None
        self.view_start = 0.0                     # edit time
        self.view_len = 0.0
        self._drag = None  # ("seek",) or ("edge", segment, which)

    # -- view
    @property
    def edit_len(self):
        return self.cuts.to_edit(self.duration)

    def set_duration(self, d):
        self.duration = d
        self.view_start, self.view_len = 0.0, self.edit_len
        self.update()

    def set_cuts(self, cuts):
        self.cuts = cuts
        self.view_len = min(self.view_len, self.edit_len)
        self.view_start = self._clamp_view(self.view_start)
        self.update()

    def set_position(self, t):
        self.position = t
        e = self.cuts.to_edit(t)
        if self._drag is None and self.view_len < self.edit_len:
            if not (self.view_start <= e <= self.view_start + self.view_len):
                self.view_start = self._clamp_view(e - self.view_len * 0.1)
        self.update()

    def zoom(self, factor, anchor=None):
        if self.duration <= 0:
            return
        anchor = self.cuts.to_edit(self.position if anchor is None else anchor)
        frac = (anchor - self.view_start) / self.view_len if self.view_len else 0.5
        self.view_len = min(self.edit_len, max(2.0, self.view_len * factor))
        self.view_start = self._clamp_view(anchor - frac * self.view_len)
        self.update()

    def zoom_fit(self):
        self.view_start, self.view_len = 0.0, self.edit_len
        self.update()

    def _clamp_view(self, vs):
        return min(max(0.0, vs), max(0.0, self.edit_len - self.view_len))

    def _plot_w(self):
        return max(1, self.width() - 2 * self.MARGIN)

    def _lanes(self):
        """(parts top, parts bottom, waveform top, waveform bottom)"""
        top = self.RULER_H + self.CLIPS_H + 8
        return top, top + self.PARTS_H, top + self.PARTS_H + 8, self.height() - 6

    def _x_for_edit(self, e):
        if self.view_len <= 0:
            return float(self.MARGIN)
        return self.MARGIN + (e - self.view_start) / self.view_len * self._plot_w()

    def x_for(self, t):
        return self._x_for_edit(self.cuts.to_edit(t))

    def t_for(self, x):
        if self.view_len <= 0:
            return 0.0
        e = self.view_start + (x - self.MARGIN) / self._plot_w() * self.view_len
        return min(self.cuts.to_source(min(max(0.0, e), self.edit_len)), self.duration)

    # -- painting
    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(28, 28, 32))
        p.fillRect(0, 0, w, self.RULER_H, QColor(42, 42, 48))
        if self.duration <= 0:
            p.setPen(QColor(140, 140, 150))
            p.drawText(self.rect(), Qt.AlignCenter, "Open or drop a video file to begin")
            return

        font = p.font()
        font.setPointSizeF(8)
        p.setFont(font)
        step = next((s for s in self.TICK_STEPS if s * self._plot_w() / self.view_len >= 90), self.TICK_STEPS[-1])
        for k in range(math.floor(self.view_start / step), math.ceil((self.view_start + self.view_len) / step) + 1):
            t = k * step
            x = self._x_for_edit(t)
            if self.MARGIN - 1 <= x <= w - self.MARGIN + 1:
                p.setPen(QColor(110, 110, 120))
                p.drawLine(QPointF(x, self.RULER_H - 7), QPointF(x, self.RULER_H))
                p.setPen(QColor(175, 175, 185))
                label = f"{t:.1f}s" if step < 1 else fmt_time(t, ms=False)
                p.drawText(QPointF(x + 3, self.RULER_H - 9), label)

        top, bot, wave_top, wave_bot = self._lanes()
        strip_top = self.RULER_H + 3
        for k, (a, b, name) in enumerate(self.clips):
            x1, x2 = self.x_for(a), self.x_for(b)
            if x2 < 0 or x1 > w:
                continue
            rect = QRectF(x1, strip_top, max(1.0, x2 - x1), self.CLIPS_H)
            p.fillRect(rect, QColor(66, 66, 80) if k % 2 == 0 else QColor(52, 52, 64))
            text_rect = rect.intersected(QRectF(0, 0, w, h)).adjusted(4, 0, -4, 0)
            if text_rect.width() > 20:
                p.setPen(QColor(200, 200, 210))
                p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft,
                           p.fontMetrics().elidedText(name, Qt.ElideRight, int(text_rect.width())))
        self._paint_wave(p, wave_top, wave_bot)
        visible = QRectF(0, 0, w, h)
        for i, seg in enumerate(self.segments):
            x1, x2 = self.x_for(seg.start), self.x_for(seg.end)
            if x2 < 0 or x1 > w:
                continue
            rect = QRectF(x1, top, max(2.0, x2 - x1), bot - top)
            color = QColor(SEG_COLORS[i % len(SEG_COLORS)])
            selected = seg is self.selected
            if not selected:
                color.setAlpha(160)
            p.fillRect(rect, color)
            p.setPen(QPen(QColor(255, 255, 255) if selected else color.darker(160), 2 if selected else 1))
            p.drawRect(rect)
            text_rect = rect.intersected(visible).adjusted(5, 0, -5, 0)
            if text_rect.width() > 24:
                p.setPen(QColor(255, 255, 255))
                p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft,
                           f"Part {i + 1}\n{fmt_time(self.cuts.span(seg.start, seg.end), ms=False)}")

        # Where one video file ends and the next starts.
        p.setPen(QPen(QColor(170, 170, 185), 1))
        for a, _, _ in self.clips[1:]:
            x = self.x_for(a)
            if self.MARGIN <= x <= w - self.MARGIN:
                p.drawLine(QPointF(x, strip_top), QPointF(x, wave_bot))

        # Where something was deleted: a thin marker at the join.
        p.setPen(QPen(QColor(240, 190, 70), 1, Qt.DashLine))
        for a, _ in self.cuts.ranges:
            x = self.x_for(a)
            if self.MARGIN <= x <= w - self.MARGIN:
                p.drawLine(QPointF(x, top), QPointF(x, wave_bot))

        if self.in_point is not None:
            x = self.x_for(self.in_point)
            p.setPen(QPen(QColor(90, 220, 120), 2))
            p.drawLine(QPointF(x, top - 4), QPointF(x, bot + 4))
            p.drawLine(QPointF(x, top - 4), QPointF(x + 6, top - 4))
            p.drawLine(QPointF(x, bot + 4), QPointF(x + 6, bot + 4))

        x = self.x_for(self.position)
        p.setPen(QPen(QColor(240, 60, 60), 2))
        p.drawLine(QPointF(x, 0), QPointF(x, h))
        p.setBrush(QColor(240, 60, 60))
        p.drawPolygon(QPolygonF([QPointF(x - 6, 0), QPointF(x + 6, 0), QPointF(x, 8)]))

    def _visible_silences(self):
        t0, t1 = self.t_for(self.MARGIN - 2), self.t_for(self.width() - self.MARGIN + 2)
        i = max(0, bisect.bisect_left(self.silences, (t0,)) - 1)
        for a, b in self.silences[i:]:
            if a > t1:
                break
            if b >= t0:
                yield a, b

    def _block_rect(self, a, b, top, bot):
        x1, x2 = self.x_for(a), self.x_for(b)
        return QRectF(x1 + 0.5, top + 2, max(2.0, x2 - x1 - 1), bot - top - 4)

    def _paint_wave(self, p, top, bot):
        lane = QRectF(self.MARGIN, top, self._plot_w(), bot - top)
        p.fillRect(lane, QColor(20, 20, 24))
        mid, half = (top + bot) / 2, (bot - top) / 2 - 1

        p.save()
        p.setClipRect(lane)
        p.setRenderHint(QPainter.Antialiasing)
        selected = set(self.selected_blocks)
        for a, b in self._visible_silences():
            rect = self._block_rect(a, b, top, bot)
            radius = min(6.0, rect.width() / 2)
            if (a, b) in selected:
                p.setPen(QPen(QColor(255, 255, 255), 2))
                p.setBrush(QColor(240, 70, 70, 200))
            else:
                p.setPen(QPen(QColor(255, 110, 110, 160), 1))
                p.setBrush(QColor(230, 50, 50, 120))
            p.drawRoundedRect(rect, radius, radius)
        p.restore()

        if self.waveform:
            cols = int(self._plot_w())
            dt = self.view_len / cols
            edges = [self.cuts.to_source(self.view_start + c * dt) for c in range(cols + 1)]
            peaks = self.waveform.column_peaks(edges, dt)
            lines = []
            for c, v in enumerate(peaks):
                x, y = self.MARGIN + c + 0.5, max(0.5, BYTE_TO_HEIGHT[v] * half)
                lines.append(QLineF(x, mid - y, x, mid + y))
            p.setPen(QColor(120, 170, 225))
            p.drawLines(lines)

        if self.wave_status:
            p.setPen(QColor(150, 150, 160))
            p.drawText(lane.adjusted(8, 0, -8, 0), Qt.AlignVCenter | Qt.AlignRight, self.wave_status)

    # -- mouse
    def _hit_edge(self, x):
        candidates = ([self.selected] if self.selected in self.segments else []) + self.segments
        for seg in candidates:
            for which in ("start", "end"):
                if abs(self.x_for(getattr(seg, which)) - x) <= self.EDGE_PX:
                    return seg, which
        return None

    def _segment_at(self, t):
        return next((s for s in reversed(self.segments) if s.start <= t <= s.end), None)

    def _block_at(self, x, y):
        top, bot = self._lanes()[2:]
        if not top <= y <= bot:
            return None
        return next((blk for blk in self._visible_silences()
                     if self._block_rect(*blk, top, bot).adjusted(-2, 0, 2, 0).contains(QPointF(x, y))), None)

    def mousePressEvent(self, e):
        if self.duration <= 0 or e.button() != Qt.LeftButton:
            return
        x, y = e.position().x(), e.position().y()
        if y > self.RULER_H:
            hit = self._hit_edge(x)
            if hit:
                self.edgeDragStarted.emit()
                self._drag = ("edge", *hit)
                if hit[0] is not self.selected:
                    self.selected = hit[0]
                    self.selectionChanged.emit(self.selected)
                self.update()
                return
            if y > self._lanes()[2]:
                blk = self._block_at(x, y)
                if e.modifiers() & Qt.ControlModifier:
                    if blk in self.selected_blocks:
                        self.selected_blocks.remove(blk)
                    elif blk:
                        self.selected_blocks.append(blk)
                else:
                    self.selected_blocks = [blk] if blk else []
                if self.selected_blocks and self.selected is not None:
                    self.selected = None
                    self.selectionChanged.emit(None)
                self.blocksChanged.emit()
            else:
                seg = self._segment_at(self.t_for(x))
                if seg is not self.selected:
                    self.selected = seg
                    self.selectionChanged.emit(seg)
                if self.selected_blocks:
                    self.selected_blocks = []
                    self.blocksChanged.emit()
        self._drag = ("seek",)
        self.seekRequested.emit(self.t_for(x))
        self.update()

    def mouseMoveEvent(self, e):
        x = e.position().x()
        if self._drag is None:
            if e.position().y() > self.RULER_H and self._hit_edge(x):
                self.setCursor(Qt.SizeHorCursor)
            elif self._block_at(x, e.position().y()):
                self.setCursor(Qt.PointingHandCursor)
            else:
                self.unsetCursor()
            return
        t = self.t_for(x)
        if self._drag[0] == "seek":
            self.seekRequested.emit(t)
        else:
            _, seg, which = self._drag
            if which == "start":
                seg.start = min(t, seg.end - 0.05)
            else:
                seg.end = max(t, seg.start + 0.05)
            self.edgeDragged.emit()
            self.seekRequested.emit(getattr(seg, which))
        self.update()

    def mouseReleaseEvent(self, e):
        drag, self._drag = self._drag, None
        if drag and drag[0] == "edge":
            self.edgeDragFinished.emit(drag[1], drag[2])

    def wheelEvent(self, e):
        if self.duration <= 0:
            return
        dx, dy = e.angleDelta().x(), e.angleDelta().y()
        if e.modifiers() & Qt.ShiftModifier or dx:
            delta = dy or dx
            self.view_start = self._clamp_view(self.view_start - delta / 120 * self.view_len * 0.1)
            self.update()
        else:
            self.zoom(0.8 ** (dy / 120), anchor=self.t_for(e.position().x()))


# ---------------------------------------------------------------- export

class Exporter(QObject):
    """Runs one ffmpeg stream-copy job per part, one after another.

    A part made of several pieces (because blocks were deleted from it) is joined in the same pass
    with the concat demuxer, still without re-encoding.
    """
    progress = Signal(float, int, int)  # overall fraction, current job index, job count
    done = Signal(bool, str)

    def __init__(self, ffmpeg, jobs, parent=None):
        super().__init__(parent)
        self.ffmpeg, self.jobs = ffmpeg, jobs  # jobs: [([(file, start, end), ...], length, out_path)]
        self.total = sum(j[1] for j in jobs) or 1.0
        self.index = 0
        self.done_len = 0.0
        self.cancelled = False
        self.proc = None
        self._buf = ""
        self._list_file = None

    def start(self):
        self._next()

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.state() != QProcess.NotRunning:
            self.proc.kill()

    def _next(self):
        if self.index >= len(self.jobs):
            self.done.emit(True, f"Exported {len(self.jobs)} part(s).")
            return
        pieces, length, out = self.jobs[self.index]
        self.progress.emit(self.done_len / self.total, self.index, len(self.jobs))
        self._buf = ""
        if len(pieces) == 1:
            src, start, _ = pieces[0]
            inputs = ["-ss", f"{start:.6f}", "-i", src, "-t", f"{length:.6f}"]
        else:
            fd, self._list_file = tempfile.mkstemp(prefix="videocutter-", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("ffconcat version 1.0\n")
                for src, a, b in pieces:
                    src = src.replace("\\", "/").replace("'", r"'\''")
                    f.write(f"file '{src}'\ninpoint {a:.6f}\noutpoint {b:.6f}\n")
            inputs = ["-f", "concat", "-safe", "0", "-i", self._list_file]
        self.proc = QProcess(self)
        self.proc.setProgram(self.ffmpeg)
        self.proc.setArguments([
            "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *inputs,
            "-map", "0:v?", "-map", "0:a?", "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-progress", "pipe:1", "-nostats", out,
        ])
        self.proc.readyReadStandardOutput.connect(self._on_output)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)
        self.proc.start()

    def _on_output(self):
        self._buf += bytes(self.proc.readAllStandardOutput()).decode(errors="ignore")
        *lines, self._buf = self._buf.split("\n")
        for line in lines:
            key, _, val = line.strip().partition("=")
            if key == "out_time_us" and val.isdigit():
                cur = min(int(val) / 1e6, self.jobs[self.index][1])
                self.progress.emit((self.done_len + cur) / self.total, self.index, len(self.jobs))

    def _on_error(self, err):
        if err == QProcess.FailedToStart:
            self.done.emit(False, "Could not start ffmpeg.")

    def _on_finished(self, code, _status):
        out = self.jobs[self.index][2]
        if self._list_file:
            Path(self._list_file).unlink(missing_ok=True)
            self._list_file = None
        if self.cancelled:
            Path(out).unlink(missing_ok=True)
            self.done.emit(False, "Export cancelled.")
            return
        if code != 0:
            err = bytes(self.proc.readAllStandardError()).decode(errors="ignore").strip()
            self.done.emit(False, f"ffmpeg failed on {Path(out).name}:\n\n{err[-1500:] or f'exit code {code}'}")
            return
        self.done_len += self.jobs[self.index][1]
        self.index += 1
        self._next()


# ---------------------------------------------------------------- main window

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 880)
        self.setAcceptDrops(True)
        self.settings = QSettings("VideoCutter", "VideoCutter")
        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = shutil.which("ffprobe")

        self.clips: list[Clip] = []
        self._offsets: list[float] = []  # where each clip starts on the timeline
        self.path = None  # first clip's file; names the exports
        self.info = {}    # first clip's info, with the size of all clips together
        self.duration = 0.0
        self.segments: list[Segment] = []
        self.cuts = Cuts()  # deleted blocks
        self.pos = 0.0
        self.exporter = None
        self._wave_loaders = {}  # path -> WaveformLoader; kept, so reordering never re-reads audio
        self._wave = None
        self._cur_clip = -1        # the clip loaded in the player
        self._pending_local = None  # position to apply once the player has loaded that clip
        self._resume_play = False
        self._updating_table = False
        self._pending_seek = None
        self._undo = []  # snapshots from _snapshot()
        self._redo = []
        self._redo_before_push = []

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.video = QVideoWidget()
        self.video.setStyleSheet("background: black;")
        self.player.setVideoOutput(self.video)
        self.player.positionChanged.connect(self._on_player_position)
        self.player.playbackStateChanged.connect(self._on_play_state)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.errorOccurred.connect(lambda _e, msg: self.statusBar().showMessage(f"Playback error: {msg}"))

        # Scrubbing fires lots of seeks; only hand the player the latest one every 40 ms.
        self._seek_timer = QTimer(self)
        self._seek_timer.setSingleShot(True)
        self._seek_timer.setInterval(40)
        self._seek_timer.timeout.connect(self._apply_seek)

        self._build_ui()
        self._build_actions()
        self._refresh_all()
        if not (self.ffmpeg and self.ffprobe):
            QTimer.singleShot(0, self._check_tools)

    # -- UI construction
    def _button(self, text, slot, tip=""):
        b = QPushButton(text)
        b.setFocusPolicy(Qt.NoFocus)
        b.setToolTip(tip)
        b.clicked.connect(slot)
        return b

    def _build_ui(self):
        style = self.style()
        self.play_btn = self._button("", self.toggle_play, "Play / pause (Space)")
        self.play_btn.setIcon(style.standardIcon(QStyle.SP_MediaPlay))
        self.time_label = QLabel()
        self.time_label.setFont(QFont("Consolas", 11))
        self.time_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.snap_cb = QCheckBox("Snap cuts to keyframes")
        self.snap_cb.setFocusPolicy(Qt.NoFocus)
        self.snap_cb.setChecked(self.settings.value("snap", True, type=bool))
        self.snap_cb.setToolTip("Lossless cuts can only start on a keyframe. Snapping makes every part\n"
                                "start exactly where you see it, with no overlap between parts.")
        self.snap_cb.toggled.connect(lambda v: self.settings.setValue("snap", v))

        self.volume = QSlider(Qt.Horizontal)
        self.volume.setFocusPolicy(Qt.NoFocus)
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(100)
        self.volume.valueChanged.connect(self._on_volume)
        self.volume.setValue(self.settings.value("volume", 70, type=int))
        self._on_volume(self.volume.value())

        controls = QHBoxLayout()
        controls.addWidget(self.play_btn)
        controls.addWidget(self.time_label)
        controls.addSpacing(16)
        controls.addWidget(self._button("⟦ In  (I)", self.set_in, "Mark where a new part starts"))
        controls.addWidget(self._button("Out (O) ⟧", self.set_out, "Mark where the new part ends and add it"))
        controls.addWidget(self._button("✂ Split  (S)", self.split_at_playhead, "Split the part under the playhead"))
        controls.addWidget(self._button("Split into N…", self.split_into_n, "Split the whole video into N equal parts (N)"))
        controls.addSpacing(10)
        controls.addWidget(self.snap_cb)
        controls.addStretch()
        controls.addWidget(QLabel("🔊"))
        controls.addWidget(self.volume)

        self.timeline = Timeline()
        self.timeline.segments = self.segments
        self.timeline.seekRequested.connect(self.seek)
        self.timeline.selectionChanged.connect(lambda _seg: self._sync_table_selection())
        self.timeline.edgeDragStarted.connect(self._push_undo)
        self.timeline.edgeDragged.connect(self._refresh_table)
        self.timeline.edgeDragFinished.connect(self._on_edge_drag_finished)
        self.timeline.blocksChanged.connect(self._on_blocks_changed)

        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(8, 8, 8, 0)
        top_layout.addWidget(self.video, 1)
        top_layout.addLayout(controls)
        top_layout.addWidget(self.timeline)
        top_layout.addLayout(self._build_silence_row())

        # parts table
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["#", "File name", "Start", "End", "Length", "Size (est.)"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.setAlternatingRowColors(True)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._on_table_selection)
        self.table.itemChanged.connect(self._on_table_edit)
        self.table.cellClicked.connect(self._on_table_click)

        table_box = QVBoxLayout()
        table_header = QHBoxLayout()
        self.parts_label = QLabel()
        table_header.addWidget(self.parts_label)
        table_header.addStretch()
        table_header.addWidget(self._button("Delete part", self.delete_part, "Delete the selected part (Delete)"))
        table_header.addWidget(self._button("Clear all", self.clear_parts))
        table_box.addLayout(table_header)
        table_box.addWidget(self.table)
        hint = QLabel("Double-click a name or time to edit it.  Help → Keyboard shortcuts for all keys.")
        hint.setStyleSheet("color: gray;")
        table_box.addWidget(hint)

        # export panel
        self.file_label = QLabel("No file loaded")
        self.file_label.setWordWrap(True)
        self.out_edit = QLineEdit()
        self.out_edit.setPlaceholderText("Same folder as the source video")
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_edit)
        out_row.addWidget(self._button("…", self.browse_output, "Choose output folder"))
        self.export_btn = self._button("Export", self.export, "Export every part losslessly (Ctrl+E)")
        self.export_btn.setMinimumHeight(40)
        f = self.export_btn.font()
        f.setPointSize(f.pointSize() + 2)
        f.setBold(True)
        self.export_btn.setFont(f)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress_label = QLabel()
        self.cancel_btn = self._button("Cancel export", self.cancel_export)
        self.open_folder_btn = self._button("Open output folder", self.open_output_folder)

        panel = QVBoxLayout()
        panel.addWidget(self.file_label)
        panel.addSpacing(8)
        panel.addWidget(QLabel("Output folder:"))
        panel.addLayout(out_row)
        panel.addSpacing(8)
        panel.addWidget(self.export_btn)
        panel.addWidget(self.progress)
        panel.addWidget(self.progress_label)
        panel.addWidget(self.cancel_btn)
        panel.addStretch()
        panel.addWidget(self.open_folder_btn)
        panel_widget = QWidget()
        panel_widget.setLayout(panel)
        panel_widget.setFixedWidth(320)

        bottom = QWidget()
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(8, 4, 8, 8)
        parts_tab = QWidget()
        parts_tab.setLayout(table_box)
        self.tabs = QTabWidget()
        self.tabs.addTab(parts_tab, "Parts")
        self.tabs.addTab(self._build_files_tab(), "Videos")
        bottom_layout.addWidget(self.tabs, 1)
        bottom_layout.addWidget(panel_widget)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(top)
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([620, 260])
        self.setCentralWidget(splitter)

    def _build_files_tab(self):
        self.files_table = QTableWidget(0, 6)
        self.files_table.setHorizontalHeaderLabels(["#", "File", "Created", "Length", "Size", "Format"])
        self.files_table.verticalHeader().setVisible(False)
        self.files_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.files_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.files_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.files_table.setAlternatingRowColors(True)
        hdr = self.files_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        self.files_table.cellDoubleClicked.connect(lambda row, _col: self.seek(self._offsets[row]))

        header = QHBoxLayout()
        self.files_label = QLabel()
        header.addWidget(self.files_label)
        header.addStretch()
        header.addWidget(self._button("Add videos…", self.add_dialog, "Add videos to the end of the timeline (Ctrl+Shift+O)"))
        header.addWidget(self._button("Sort by date created", self.sort_by_created,
                                      "Put the videos on the timeline in the order their files were created"))
        header.addWidget(self._button("▲", lambda: self.move_clip(-1), "Move the selected video earlier"))
        header.addWidget(self._button("▼", lambda: self.move_clip(1), "Move the selected video later"))
        header.addWidget(self._button("Remove", self.remove_clip, "Take the selected video off the timeline"))

        self.files_warning = QLabel()
        self.files_warning.setWordWrap(True)
        self.files_warning.setStyleSheet("color: rgb(240, 190, 70);")
        hint = QLabel("The videos play back to back on the timeline, top to bottom. Double-click one to jump to it.")
        hint.setStyleSheet("color: gray;")

        box = QVBoxLayout()
        box.addLayout(header)
        box.addWidget(self.files_table)
        box.addWidget(self.files_warning)
        box.addWidget(hint)
        tab = QWidget()
        tab.setLayout(box)
        return tab

    def _build_silence_row(self):
        self.silence_btn = self._button("Highlight silence", self._update_silences,
                                        "Mark in red where the audio is (nearly) silent")
        self.silence_btn.setCheckable(True)
        self.silence_btn.setStyleSheet("QPushButton:checked { background: rgb(170, 45, 45); }")
        self.silence_btn.setChecked(self.settings.value("silence_on", False, type=bool))

        self.silence_len = QDoubleSpinBox()
        self.silence_len.setRange(0.05, 600)
        self.silence_len.setDecimals(2)
        self.silence_len.setSingleStep(0.25)
        self.silence_len.setSuffix(" s")
        self.silence_len.setValue(self.settings.value("silence_min", 10.0, type=float))
        self.silence_len.setToolTip("Quiet stretches shorter than this are ignored")

        self.silence_db = QSpinBox()
        self.silence_db.setRange(-90, -10)
        self.silence_db.setSuffix(" dB")
        self.silence_db.setValue(self.settings.value("silence_db", -50, type=int))
        self.silence_db.setToolTip("Audio quieter than this counts as silence.\n"
                                   "Raise it (e.g. -40) if background noise keeps silence from being found.")

        for box in (self.silence_len, self.silence_db):
            box.setKeyboardTracking(False)
            box.setFocusPolicy(Qt.ClickFocus)  # don't take the keyboard at startup
            box.valueChanged.connect(self._update_silences)
            box.editingFinished.connect(box.clearFocus)  # give the keyboard back to the shortcuts

        self.silence_label = QLabel()
        self.silence_label.setStyleSheet("color: gray;")
        self.silence_label.setMinimumWidth(240)
        self.select_blocks_btn = self._button("Select all", self.select_all_blocks,
                                              "Select every red block (Ctrl+A). Ctrl+click a block to add or remove it.")
        self.delete_blocks_btn = self._button("Delete blocks", self.delete_blocks,
                                              "Cut the selected blocks out of the video and close the gaps (Delete)")

        row = QHBoxLayout()
        row.addWidget(self.silence_btn)
        row.addSpacing(8)
        row.addWidget(QLabel("At least"))
        row.addWidget(self.silence_len)
        row.addSpacing(8)
        row.addWidget(QLabel("Quieter than"))
        row.addWidget(self.silence_db)
        row.addSpacing(12)
        row.addWidget(self.silence_label)
        row.addStretch()
        row.addWidget(self.select_blocks_btn)
        row.addWidget(self.delete_blocks_btn)
        return row

    def _build_actions(self):
        menu_file = self.menuBar().addMenu("&File")
        menu_edit = self.menuBar().addMenu("&Edit")
        menu_play = self.menuBar().addMenu("&Playback")
        menu_view = self.menuBar().addMenu("&View")
        menu_help = self.menuBar().addMenu("&Help")

        def act(menu, text, keys, slot):
            a = QAction(text, self)
            a.setShortcuts([QKeySequence(k) for k in (keys if isinstance(keys, list) else [keys])] if keys else [])
            a.triggered.connect(slot)
            self.addAction(a)
            if menu:
                menu.addAction(a)
            return a

        act(menu_file, "&Open videos…", "Ctrl+O", self.open_dialog)
        act(menu_file, "&Add videos…", "Ctrl+Shift+O", self.add_dialog)
        act(menu_file, "&Export parts", "Ctrl+E", self.export)
        menu_file.addSeparator()
        act(menu_file, "E&xit", "Ctrl+Q", self.close)

        self.undo_action = act(menu_edit, "&Undo", "Ctrl+Z", self.undo)
        self.redo_action = act(menu_edit, "&Redo", ["Ctrl+Y", "Ctrl+Shift+Z"], self.redo)
        menu_edit.addSeparator()
        act(menu_edit, "Split at playhead", "S", self.split_at_playhead)
        act(menu_edit, "Split into N equal parts…", "N", self.split_into_n)
        act(menu_edit, "Set in point", "I", self.set_in)
        act(menu_edit, "Set out point (adds part)", "O", self.set_out)
        act(menu_edit, "Delete selected part / blocks", "Delete", self.delete_selected)
        act(menu_edit, "Select all silent blocks", "Ctrl+A", self.select_all_blocks)
        act(menu_edit, "Clear all parts", None, self.clear_parts)
        act(None, "Clear in point / selection", "Esc", self.clear_marks)

        act(menu_play, "Play / pause", "Space", self.toggle_play)
        act(menu_play, "Go to time…", "Ctrl+G", self.go_to_time)
        menu_play.addSeparator()
        act(menu_play, "Back 5 s", "Left", lambda: self.nudge(-5))
        act(menu_play, "Forward 5 s", "Right", lambda: self.nudge(5))
        act(menu_play, "Back 1 min", "Shift+Left", lambda: self.nudge(-60))
        act(menu_play, "Forward 1 min", "Shift+Right", lambda: self.nudge(60))
        act(menu_play, "Back 10 min", "PgUp", lambda: self.nudge(-600))
        act(menu_play, "Forward 10 min", "PgDown", lambda: self.nudge(600))
        act(menu_play, "Previous frame", ",", lambda: self.step_frames(-1))
        act(menu_play, "Next frame", ".", lambda: self.step_frames(1))
        act(menu_play, "Previous cut point", "[", lambda: self.jump_cut(-1))
        act(menu_play, "Next cut point", "]", lambda: self.jump_cut(1))
        act(menu_play, "Go to start", "Home", lambda: self.seek(0))
        act(menu_play, "Go to end", "End", lambda: self.seek(self.duration))

        act(menu_view, "Zoom timeline to fit", "Ctrl+0", self.timeline.zoom_fit)
        act(menu_view, "Zoom in", ["Ctrl+=", "Ctrl++"], lambda: self.timeline.zoom(0.5))
        act(menu_view, "Zoom out", "Ctrl+-", lambda: self.timeline.zoom(2.0))

        act(menu_help, "Keyboard shortcuts", "F1", self.show_shortcuts)

    # -- file loading
    def _check_tools(self):
        if self.ffmpeg and self.ffprobe:
            return True
        QMessageBox.critical(self, APP_NAME, "ffmpeg and ffprobe were not found on your PATH.\n\n"
                                             "Install them (e.g. 'winget install Gyan.FFmpeg') and restart.")
        return False

    def open_dialog(self):
        self._pick_files(add=False)

    def add_dialog(self):
        self._pick_files(add=bool(self.clips))

    def _pick_files(self, add):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add videos" if add else "Open videos",
                                                self.settings.value("last_dir", ""),
                                                f"Video files ({VIDEO_EXTS});;All files (*)")
        if paths:
            self.open_files(paths, add)

    def open_files(self, paths, add=False):
        """Open videos onto the timeline, replacing what's there, or appending them when add is True."""
        if self.exporter:
            QMessageBox.information(self, APP_NAME, "Wait for the export to finish first.")
            return
        if not self._check_tools():
            return
        if not add and (self.segments or self.cuts) and QMessageBox.question(
                self, APP_NAME, "Discard the current parts and open new videos?") != QMessageBox.Yes:
            return
        have = {os.path.normcase(os.path.abspath(c.path)) for c in self.clips} if add else set()
        new, errors = [], []
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for path in paths:
                key = os.path.normcase(os.path.abspath(path))
                if key in have:
                    errors.append(f"{Path(path).name}: already on the timeline")
                    continue
                try:
                    info = probe(self.ffprobe, path)
                except Exception as e:
                    errors.append(f"{Path(path).name}: {e}")
                    continue
                if info["duration"] <= 0:
                    errors.append(f"{Path(path).name}: couldn't determine its duration")
                    continue
                have.add(key)
                new.append(Clip(path, info))
        finally:
            QApplication.restoreOverrideCursor()
        if errors:
            QMessageBox.warning(self, APP_NAME, "Some files were skipped:\n\n" + "\n".join(errors[:15]))
        if not new:
            return

        if add:
            self.clips.extend(new)  # appending leaves every existing time where it was
        else:
            self.clips[:] = new
            self.segments.clear()
            self._undo.clear()
            self._redo.clear()
            self.timeline.selected = None
            self.timeline.in_point = None
            self.timeline.selected_blocks = []
            self._set_cuts(Cuts())
            self.pos = 0.0
        if not add or not self.out_edit.text().strip():
            self.out_edit.setText(str(Path(new[0].path).parent))
        self.settings.setValue("last_dir", str(Path(new[-1].path).parent))
        self._clips_changed()
        self.statusBar().showMessage(
            f"Added {len(new)} video{'s' if len(new) != 1 else ''}." if add else
            "Loaded. Press S to split at the playhead, or I / O to mark a part.", 8000)

    def _clips_changed(self):
        """Rebuild everything that depends on which videos are on the timeline and in what order."""
        self._offsets = list(itertools.accumulate((c.duration for c in self.clips), initial=0.0))
        self.duration = self._offsets.pop() if self.clips else 0.0
        first = self.clips[0] if self.clips else None
        self.path = first.path if first else None
        self.info = dict(first.info, size=sum(c.info["size"] for c in self.clips)) if first else {}
        self.timeline.clips = [(off, off + c.duration, c.name) for off, c in zip(self._offsets, self.clips)]
        self.timeline.set_duration(self.duration)
        self._cur_clip, self._pending_local = -1, None
        if not self.clips:
            self.player.stop()
            self.player.setSource(QUrl())
        self.setWindowTitle(f"{first.name}{f' + {len(self.clips) - 1} more' if len(self.clips) > 1 else ''}"
                            f" — {APP_NAME}" if first else APP_NAME)
        self._reset_waveform()
        self._refresh_files()
        self._refresh_all()
        self.pos = min(self.pos, self.duration)
        if self.clips:
            self._player_seek(self.pos, play=False)
            self.timeline.set_position(self.pos)

    def _refresh_files(self):
        t = self.files_table
        t.setRowCount(len(self.clips))
        for i, c in enumerate(self.clips):
            created = datetime.datetime.fromtimestamp(c.info["created"]).strftime("%Y-%m-%d  %H:%M:%S")
            values = [str(i + 1), c.name, created, fmt_time(c.duration, ms=False), fmt_size(c.info["size"]), c.format]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setTextAlignment(Qt.AlignCenter)
                elif col in (3, 4):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if col == 1:
                    item.setToolTip(c.path)
                t.setItem(i, col, item)
        n = len(self.clips)
        self.tabs.setTabText(1, f"Videos ({n})" if n else "Videos")
        self.files_label.setText(f"<b>{n} video{'s' if n != 1 else ''}</b>  ·  {fmt_time(self.duration, ms=False)}"
                                 if n else "<b>Videos</b> — open or drop videos to put them on the timeline")
        formats = {(c.info["vcodec"], c.info["width"], c.info["height"], round(c.info["fps"], 2), c.info["acodec"])
                   for c in self.clips}
        self.files_warning.setText(
            "⚠ These videos aren't all the same format (codec, size or frame rate). Exporting anything that "
            "spans more than one of them without re-encoding will probably give a broken file." if len(formats) > 1 else "")
        self.files_warning.setVisible(len(formats) > 1)

    def _selected_clip(self):
        rows = self.files_table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def _set_clip_order(self, clips, select=None):
        """Change which videos are on the timeline or their order. Parts and deleted blocks can't follow."""
        if clips == self.clips:
            return False
        if (self.segments or self.cuts or self.timeline.in_point is not None) and QMessageBox.question(
                self, APP_NAME, "Changing the videos or their order clears your parts and deleted blocks, "
                                "and this can't be undone. Continue?") != QMessageBox.Yes:
            return False
        self.segments.clear()
        self._undo.clear()
        self._redo.clear()
        self.timeline.selected = None
        self.timeline.in_point = None
        self.timeline.selected_blocks = []
        self._set_cuts(Cuts())
        self.clips[:] = clips
        self.pos = 0.0
        self._clips_changed()
        if select is not None:
            self.files_table.selectRow(select)
        return True

    def sort_by_created(self):
        ordered = sorted(self.clips, key=lambda c: c.info["created"])
        if ordered == self.clips:
            self.statusBar().showMessage("The videos are already in the order they were created.", 4000)
        elif self._set_clip_order(ordered):
            self.statusBar().showMessage("Sorted the videos by date created.", 4000)

    def move_clip(self, step):
        i = self._selected_clip()
        if i is None or not 0 <= i + step < len(self.clips):
            return
        clips = list(self.clips)
        clips[i], clips[i + step] = clips[i + step], clips[i]
        self._set_clip_order(clips, select=i + step)

    def remove_clip(self):
        i = self._selected_clip()
        if i is not None:
            self._set_clip_order(self.clips[:i] + self.clips[i + 1:])

    def _clip_at(self, t):
        """(index of the clip playing at timeline time t, time within that clip)"""
        i = min(max(0, bisect.bisect_right(self._offsets, t) - 1), len(self.clips) - 1)
        return i, t - self._offsets[i]

    def _file_pieces(self, a, b):
        """Timeline range a..b as (file, start, end) pieces, split where one file ends and the next starts."""
        out = []
        for off, c in zip(self._offsets, self.clips):
            lo, hi = max(a, off), min(b, off + c.duration)
            if hi - lo > 1e-6:
                out.append((c.path, lo - off, hi - off))
        return out

    def browse_output(self):
        d = QFileDialog.getExistingDirectory(self, "Output folder", self.out_edit.text())
        if d:
            self.out_edit.setText(d)

    def _output_dir(self):
        text = self.out_edit.text().strip()
        if text:
            return Path(text)
        return Path(self.path).parent if self.path else None

    def open_output_folder(self):
        d = self._output_dir()
        if d and d.is_dir():
            os.startfile(d)

    # -- waveform / silence
    def _stop_waveform(self):
        for loader in self._wave_loaders.values():
            loader.cancel()
            loader.deleteLater()
        self._wave_loaders.clear()

    def _reset_waveform(self):
        """Start the timeline's waveform over, reusing whatever audio was already read for each file."""
        self._wave = Waveform() if self.clips else None
        self._wave_k, self._wave_taken = 0, 0  # clips fully copied in, bytes copied from the next one
        self.timeline.waveform = self._wave
        self._wave_clock = QElapsedTimer()
        self._wave_clock.start()
        self._pump_wave_loaders()
        self._extend_waveform()

    def _pump_wave_loaders(self):
        """Read one file's audio at a time, in timeline order."""
        if any(not ld.complete for ld in self._wave_loaders.values()):
            return
        for c in self.clips:
            if c.path not in self._wave_loaders:
                loader = WaveformLoader(self.ffmpeg, c.path, self)
                loader.updated.connect(self._extend_waveform)
                loader.done.connect(self._on_wave_loader_done)
                self._wave_loaders[c.path] = loader
                loader.start()
                return

    def _on_wave_loader_done(self, *_):
        self._pump_wave_loaders()
        self._extend_waveform()

    def _extend_waveform(self):
        """Copy newly read audio into the timeline's waveform, file by file in timeline order."""
        wave = self._wave
        if wave is None:
            return
        while self._wave_k < len(self.clips):
            clip = self.clips[self._wave_k]
            loader = self._wave_loaders.get(clip.path)
            if loader is None:
                break
            target = round(clip.duration * Waveform.RATE)  # exactly the clip's length, so files line up
            data = loader.wave.levels[0]
            avail = min(len(data), target)
            if avail > self._wave_taken:
                wave.append(bytes(data[self._wave_taken:avail]))
                self._wave_taken = avail
            if not loader.complete:
                break
            if self._wave_taken < target:
                wave.append(bytes(target - self._wave_taken))
            self._wave_k += 1
            self._wave_taken = 0
        done = self._wave_k >= len(self.clips)
        self.timeline.wave_status = "" if done else \
            f"Reading audio… {min(99, wave.loaded / max(self.duration, 1e-9) * 100):.0f}%"
        if done or self._wave_clock.elapsed() > 1000:  # keep the silence marks current without redoing them constantly
            self._wave_clock.restart()
            self._update_silences()
        self.timeline.update()

    def _update_silences(self, *_):
        on = self.silence_btn.isChecked()
        self.settings.setValue("silence_on", on)
        self.settings.setValue("silence_min", self.silence_len.value())
        self.settings.setValue("silence_db", self.silence_db.value())
        wave = self.timeline.waveform
        min_len = self.silence_len.value()
        sil = wave.silences(self.silence_db.value(), min_len) if on and wave else []
        if self.cuts:  # leave out what was already deleted
            sil = [piece for a, b in sil for piece in self.cuts.kept(a, b) if piece[1] - piece[0] >= min_len]
        self.timeline.silences = sil
        keep = set(sil)
        self.timeline.selected_blocks = [b for b in self.timeline.selected_blocks if b in keep]
        if not on or not wave:
            self.silence_label.setText("")
        elif sil:
            total = sum(b - a for a, b in sil)
            self.silence_label.setText(f"{len(sil)} silent stretch{'es' if len(sil) != 1 else ''} · "
                                       f"{fmt_time(total, ms=False)} total")
        else:
            self.silence_label.setText("No silence found")
        self._on_blocks_changed()

    def _on_blocks_changed(self):
        blocks = self.timeline.selected_blocks
        self.select_blocks_btn.setEnabled(bool(self.timeline.silences))
        self.delete_blocks_btn.setEnabled(bool(blocks))
        self.delete_blocks_btn.setText(f"Delete {len(blocks)} block{'s' if len(blocks) != 1 else ''}"
                                       if blocks else "Delete blocks")
        if blocks:
            self._sync_table_selection()
        self.timeline.update()

    def select_all_blocks(self):
        if not self.timeline.silences:
            self.statusBar().showMessage("Turn on Highlight silence to get blocks to select.", 4000)
            return
        self.timeline.selected_blocks = list(self.timeline.silences)
        self.timeline.selected = None
        self._on_blocks_changed()

    def _set_cuts(self, cuts):
        self.cuts = cuts
        self.timeline.set_cuts(cuts)

    def delete_blocks(self):
        """Cut the selected silent blocks out of the video, so what's left and right of each joins up."""
        blocks = sorted(self.timeline.selected_blocks)
        if not blocks or not self._has_media():
            return
        # The video after a block has to restart on a keyframe, so end each cut on the last keyframe
        # inside the block. That keeps a little of the silence but never touches the sound around it.
        snap = self.snap_cb.isChecked()
        ranges, skipped = [], 0
        if snap:
            QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for a, b in blocks:
                if snap and b < self.duration - 0.001:
                    end = self._clean_restart(a, b)
                    if end is None or end - a < 0.05:
                        skipped += 1
                        continue
                    b = end
                ranges.append((a, b))
        finally:
            if snap:
                QApplication.restoreOverrideCursor()

        if ranges:
            self._push_undo()
            self._set_cuts(self.cuts.add(ranges))
            for seg in list(self.segments):
                seg.start = self.cuts.skip(seg.start)
                inside = self.cuts.containing(seg.end - 1e-6)
                if inside:
                    seg.end = inside[0]
                if self.cuts.span(seg.start, seg.end) < 0.05:
                    self.segments.remove(seg)
            if self.timeline.in_point is not None:
                self.timeline.in_point = self.cuts.skip(self.timeline.in_point)
            self.timeline.selected_blocks = []
            self._update_silences()
            self.seek(self.pos)
            self._parts_changed()
        removed = sum(b - a for a, b in ranges)
        msg = f"Deleted {len(ranges)} block{'s' if len(ranges) != 1 else ''} ({fmt_time(removed)})."
        if skipped:
            msg += (f" {skipped} had no keyframe inside, so they can't be cut cleanly — "
                    "turn off 'Snap cuts to keyframes' to delete them anyway.")
        self.statusBar().showMessage(msg, 10000)

    def _clean_restart(self, a, b):
        """The latest point in a..b where playback can restart without re-encoding, or None."""
        i, local = self._clip_at(b)
        if local < 1e-3:
            return b  # the start of a file
        clip, off = self.clips[i], self._offsets[i]
        if not clip.info["vcodec"]:
            return b  # audio only: any point will do
        try:
            kf = last_keyframe_in(self.ffprobe, clip.path, max(0.0, a - off), local)
        except Exception:
            kf = None
        if kf is not None:
            return off + kf
        return off if a < off else None

    # -- playback
    def _has_media(self):
        return self.path is not None and self.duration > 0

    def _on_volume(self, v):
        self.audio.setVolume(v / 100)
        self.settings.setValue("volume", v)

    def toggle_play(self):
        if not self._has_media():
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _on_play_state(self, state):
        icon = QStyle.SP_MediaPause if state == QMediaPlayer.PlayingState else QStyle.SP_MediaPlay
        self.play_btn.setIcon(self.style().standardIcon(icon))

    def seek(self, t):
        if not self._has_media():
            return
        self.pos = self.cuts.skip(min(max(0.0, t), self.duration))
        self.timeline.set_position(self.pos)
        self._update_time_label()
        self._pending_seek = self.pos
        if not self._seek_timer.isActive():
            self._seek_timer.start()

    def _apply_seek(self):
        if self._pending_seek is not None:
            self._player_seek(self._pending_seek)
            self._pending_seek = None

    def _player_seek(self, t, play=None):
        """Show timeline time t in the player, switching to the right file if needed."""
        i, local = self._clip_at(t)
        if i != self._cur_clip:
            self._resume_play = self.player.playbackState() == QMediaPlayer.PlayingState if play is None else play
            self._cur_clip, self._pending_local = i, local
            self.player.stop()  # switching while playing would start the new file from 0
            self.player.setSource(QUrl.fromLocalFile(self.clips[i].path))
            if self._pending_local is not None:  # not loaded yet: this loads it and shows a frame
                self.player.pause()
        elif self._pending_local is not None:
            self._pending_local = local
        else:
            self.player.setPosition(int(round(local * 1000)))
            if play:
                self.player.play()

    def _apply_pending_local(self):
        ready = (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferingMedia, QMediaPlayer.BufferedMedia)
        if self._pending_local is None or self.player.mediaStatus() not in ready:
            return  # the next status change will try again
        local, self._pending_local = self._pending_local, None
        # A freshly loaded file is "stopped", which ignores positions; and playing right after
        # a seek restarts from 0. So get it paused or playing first, then seek.
        if self._resume_play:
            self.player.play()
        else:
            self.player.pause()
        self.player.setPosition(int(round(local * 1000)))
        self._resume_play = False

    def _on_media_status(self, status):
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia) and self._pending_local is not None:
            # Not from inside the player's own signal: a position set there gets lost.
            QTimer.singleShot(0, self._apply_pending_local)
        elif status == QMediaPlayer.EndOfMedia and 0 <= self._cur_clip < len(self.clips) - 1:
            nxt = self.cuts.skip(self._offsets[self._cur_clip + 1])  # carry on with the next file
            if nxt < self.duration:
                self.pos = nxt
                self.timeline.set_position(nxt)
                self._update_time_label()
                self._player_seek(nxt, play=True)

    def _on_player_position(self, ms):
        # Ignore stale positions while a seek is pending or the user is dragging.
        if self._seek_timer.isActive() or self.timeline._drag is not None or self._pending_local is not None:
            return
        if not 0 <= self._cur_clip < len(self.clips):
            return
        i = self._cur_clip
        self.pos = min(self._offsets[i] + ms / 1000, self._offsets[i] + self.clips[i].duration)
        deleted = self.cuts.containing(self.pos)
        if deleted and self.player.playbackState() == QMediaPlayer.PlayingState:
            self.pos = deleted[1]
            self._player_seek(min(self.duration, deleted[1] + 0.02))  # hop over the deleted block
        self.timeline.set_position(self.pos)
        self._update_time_label()

    def _edit_seek(self, e):
        """Seek to a time on the edited (shortened) video."""
        self.seek(self.cuts.to_source(min(max(0.0, e), self.cuts.to_edit(self.duration))))

    def nudge(self, seconds):
        self._edit_seek(self.cuts.to_edit(self.pos) + seconds)

    def step_frames(self, n):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        self._edit_seek(self.cuts.to_edit(self.pos) + n / self.info.get("fps", 30.0))

    def jump_cut(self, direction):
        points = sorted({0.0, self.duration, *(s.start for s in self.segments), *(s.end for s in self.segments),
                         *(b for _, b in self.cuts.ranges)}
                        | ({self.timeline.in_point} if self.timeline.in_point is not None else set()))
        if direction < 0:
            target = next((p for p in reversed(points) if p < self.pos - 0.01), 0.0)
        else:
            target = next((p for p in points if p > self.pos + 0.01), self.duration)
        self.seek(target)

    def go_to_time(self):
        if not self._has_media():
            return
        text, ok = QInputDialog.getText(self, "Go to time", "Time (h:mm:ss.ms, mm:ss, or seconds):",
                                        text=fmt_time(self.cuts.to_edit(self.pos)))
        if ok:
            t = parse_time(text)
            if t is None:
                self.statusBar().showMessage("Couldn't read that time.", 4000)
            else:
                self._edit_seek(t)

    def _update_time_label(self):
        self.time_label.setText(f"{fmt_time(self.cuts.to_edit(self.pos))} / {fmt_time(self.cuts.to_edit(self.duration))}")

    # -- undo / redo
    def _snapshot(self):
        sel = self.timeline.selected
        return ([(s.start, s.end, s.name) for s in self.segments], self.timeline.in_point, self.cuts.ranges,
                self.segments.index(sel) if sel in self.segments else -1)

    def _push_undo(self):
        """Call right before changing the parts or the in point."""
        self._undo.append(self._snapshot())
        del self._undo[:-200]
        self._redo_before_push, self._redo = self._redo, []

    def _drop_noop_undo(self):
        """Undo the last _push_undo if nothing actually changed since."""
        if self._undo and self._undo[-1][:3] == self._snapshot()[:3]:
            self._undo.pop()
            self._redo = self._redo_before_push

    def _restore(self, snap):
        parts, in_point, cuts, sel = snap
        self.segments[:] = [Segment(*p) for p in parts]
        self.timeline.in_point = in_point
        self.timeline.selected = self.segments[sel] if 0 <= sel < len(self.segments) else None
        if cuts != self.cuts.ranges:
            self._set_cuts(Cuts(cuts))
            self._update_silences()
            self.seek(self.pos)
        self._refresh_all()

    def undo(self):
        if not self._undo:
            self.statusBar().showMessage("Nothing to undo.", 3000)
            return
        self._redo.append(self._snapshot())
        self._restore(self._undo.pop())
        self.statusBar().showMessage("Undone.", 3000)

    def redo(self):
        if not self._redo:
            self.statusBar().showMessage("Nothing to redo.", 3000)
            return
        self._undo.append(self._snapshot())
        self._restore(self._redo.pop())
        self.statusBar().showMessage("Redone.", 3000)

    # -- editing parts
    def snap(self, t):
        """Move t to the nearest keyframe (if snapping is on), so stream-copy cuts are exact."""
        if not self.snap_cb.isChecked() or t <= 0.001 or t >= self.duration - 0.001:
            return t
        i, local = self._clip_at(t)
        clip, off = self.clips[i], self._offsets[i]
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            kf = nearest_keyframe(self.ffprobe, clip.path, local)
        except Exception:
            kf = None
        finally:
            QApplication.restoreOverrideCursor()
        if kf is None:
            return self.cuts.skip(t)
        # The start of each file is a clean cut point too.
        return self.cuts.skip(min((off + min(max(0.0, kf), clip.duration), off, off + clip.duration),
                                  key=lambda c: abs(c - t)))

    def _parts_changed(self, select=None):
        self.segments.sort(key=lambda s: (s.start, s.end))
        if select is not None:
            self.timeline.selected = select
        if self.timeline.selected not in self.segments:
            self.timeline.selected = None
        self._refresh_all()

    def split_at_playhead(self):
        if not self._has_media():
            return
        t = self.snap(self.pos)
        if t <= 0.05 or t >= self.duration - 0.05:
            self.statusBar().showMessage("Move the playhead away from the very start/end to split.", 4000)
            return
        if not self.segments:
            self._push_undo()
            first, new = Segment(0.0, t), Segment(t, self.duration)
            self.segments.extend([first, new])
        else:
            seg = next((s for s in self.segments if s.start + 0.05 < t < s.end - 0.05), None)
            if seg is None:
                self.statusBar().showMessage("The playhead isn't inside a part — nothing to split.", 4000)
                return
            self._push_undo()
            new = Segment(t, seg.end)
            seg.end = t
            self.segments.append(new)
        self.seek(t)
        self._parts_changed(select=new)
        self.statusBar().showMessage(f"Split at {fmt_time(t)}", 4000)

    def split_into_n(self):
        if not self._has_media():
            return
        n, ok = QInputDialog.getInt(self, "Split into equal parts", "Number of parts:", 3, 2, 500)
        if not ok:
            return
        if self.segments and QMessageBox.question(
                self, APP_NAME, "Replace the current parts?") != QMessageBox.Yes:
            return
        length = self.cuts.to_edit(self.duration)
        points = [0.0] + [self.snap(self.cuts.to_source(length * k / n)) for k in range(1, n)] + [self.duration]
        points = sorted(set(points))
        self._push_undo()
        self.segments.clear()
        self.segments.extend(Segment(a, b) for a, b in zip(points, points[1:]) if self.cuts.span(a, b) > 0.05)
        self._parts_changed(select=self.segments[0] if self.segments else None)
        self.statusBar().showMessage(f"Split into {len(self.segments)} parts.", 4000)

    def set_in(self):
        if not self._has_media():
            return
        t = self.snap(self.pos)
        if t != self.timeline.in_point:
            self._push_undo()
        self.timeline.in_point = t
        self.seek(self.timeline.in_point)
        self.timeline.update()
        self.statusBar().showMessage(f"In point at {fmt_time(self.timeline.in_point)} — now move to the end and press O.", 6000)

    def set_out(self):
        if not self._has_media():
            return
        start = self.timeline.in_point
        if start is None:
            self.statusBar().showMessage("Press I to mark where the part starts first.", 4000)
            return
        end = self.snap(self.pos)
        if end <= start + 0.05:
            self.statusBar().showMessage("The out point must be after the in point.", 4000)
            return
        self._push_undo()
        seg = Segment(start, end)
        self.segments.append(seg)
        self.timeline.in_point = None
        self.seek(end)
        self._parts_changed(select=seg)
        self.statusBar().showMessage(f"Added part {fmt_time(start)} → {fmt_time(end)}", 4000)

    def delete_selected(self):
        if self.timeline.selected_blocks:
            self.delete_blocks()
        else:
            self.delete_part()

    def delete_part(self):
        seg = self.timeline.selected
        if seg not in self.segments:
            return
        self._push_undo()
        i = self.segments.index(seg)
        self.segments.remove(seg)
        nxt = self.segments[min(i, len(self.segments) - 1)] if self.segments else None
        self._parts_changed(select=nxt)

    def clear_parts(self):
        if self.segments and QMessageBox.question(self, APP_NAME, "Remove all parts?") == QMessageBox.Yes:
            self._push_undo()
            self.segments.clear()
            self._parts_changed()

    def clear_marks(self):
        if self.timeline.in_point is not None:
            self._push_undo()
        self.timeline.in_point = None
        self.timeline.selected = None
        self.timeline.selected_blocks = []
        self._on_blocks_changed()
        self._refresh_all()

    def _on_edge_drag_finished(self, seg, which):
        t = self.snap(getattr(seg, which))
        if (which == "start" and t < seg.end - 0.05) or (which == "end" and t > seg.start + 0.05):
            setattr(seg, which, t)
        self._drop_noop_undo()  # a click on an edge without moving it
        self.seek(getattr(seg, which))
        self._parts_changed(select=seg)

    # -- table
    def _default_name(self, i):
        return f"{Path(self.path).stem} - part{i + 1}" if self.path else f"part{i + 1}"

    def _refresh_table(self):
        self._updating_table = True
        self.table.setRowCount(len(self.segments))
        size = self.info.get("size", 0)
        for i, seg in enumerate(self.segments):
            length = self.cuts.span(seg.start, seg.end)
            est = length / self.duration * size if self.duration else 0
            values = [str(i + 1), seg.name or self._default_name(i), fmt_time(self.cuts.to_edit(seg.start)),
                      fmt_time(self.cuts.to_edit(seg.end)), fmt_time(length), "~" + fmt_size(est)]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col not in (1, 2, 3):
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if col == 1 and seg.name is None:
                    item.setForeground(QColor(160, 160, 170))
                if col == 0:
                    item.setTextAlignment(Qt.AlignCenter)
                    item.setBackground(SEG_COLORS[i % len(SEG_COLORS)])
                elif col >= 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    item.setFont(QFont("Consolas", 10))
                self.table.setItem(i, col, item)
        self._updating_table = False
        self._sync_table_selection()

    def _sync_table_selection(self):
        self._updating_table = True
        seg = self.timeline.selected
        if seg in self.segments:
            self.table.selectRow(self.segments.index(seg))
        else:
            self.table.clearSelection()
        self._updating_table = False
        self.timeline.update()

    def _on_table_selection(self):
        if self._updating_table:
            return
        rows = self.table.selectionModel().selectedRows()
        self.timeline.selected = self.segments[rows[0].row()] if rows else None
        if rows and self.timeline.selected_blocks:
            self.timeline.selected_blocks = []
            self._on_blocks_changed()
        self.timeline.update()

    def _on_table_click(self, row, col):
        if 0 <= row < len(self.segments):
            seg = self.segments[row]
            self.seek(seg.end if col == 3 else seg.start)

    def _on_table_edit(self, item):
        if self._updating_table:
            return
        row, col = item.row(), item.column()
        if not 0 <= row < len(self.segments):
            return
        seg = self.segments[row]
        text = item.text().strip()
        self._push_undo()
        if col == 1:
            seg.name = None if not text or text == self._default_name(row) else text
        elif col in (2, 3):
            t = parse_time(text)
            if t is None:
                self.statusBar().showMessage("Couldn't read that time — use h:mm:ss.ms", 4000)
            else:
                t = self.snap(self.cuts.to_source(min(t, self.cuts.to_edit(self.duration))))
                if col == 2 and t < seg.end - 0.05:
                    seg.start = t
                elif col == 3 and t > seg.start + 0.05:
                    seg.end = t
                else:
                    self.statusBar().showMessage("A part's start must be before its end.", 4000)
                self.seek(getattr(seg, "start" if col == 2 else "end"))
        self._drop_noop_undo()
        # Defer the rebuild: we're inside the table's own edit-commit signal.
        QTimer.singleShot(0, lambda: self._parts_changed(select=seg))

    def _refresh_all(self):
        self._refresh_table()
        self.timeline.update()
        self._update_time_label()
        n = len(self.segments)
        total = sum(self.cuts.span(s.start, s.end) for s in self.segments)
        self.parts_label.setText(f"<b>Parts: {n}</b>  ·  total {fmt_time(total, ms=False)}" if n else
                                 "<b>Parts</b> — press <b>S</b> to split at the playhead, or <b>I</b>/<b>O</b> to mark a part")
        busy = self.exporter is not None
        joined = len(self.clips) > 1
        self.export_btn.setText(f"Export {n} part{'s' if n != 1 else ''}" if n else
                                "Export edited video" if self.cuts else "Export joined video" if joined else "Export")
        self.export_btn.setEnabled(bool(n or self.cuts or joined) and not busy)
        self.cancel_btn.setVisible(busy)
        self.progress.setVisible(busy)
        if self.path:
            i = self.info
            self.file_label.setText(
                (f"<b>{len(self.clips)} videos</b> starting with {Path(self.path).name}<br>"
                 if len(self.clips) > 1 else f"<b>{Path(self.path).name}</b><br>")
                + f"{i['width']}×{i['height']} · {i['fps']:.3g} fps · "
                f"{fmt_size(i['size'])} · {fmt_time(self.duration, ms=False)}"
                + (f"<br>Edited: {fmt_time(self.cuts.to_edit(self.duration), ms=False)} "
                   f"({len(self.cuts.ranges)} block{'s' if len(self.cuts.ranges) != 1 else ''} deleted)"
                   if self.cuts else ""))

    # -- export
    def export(self):
        if not self._has_media() or self.exporter:
            return
        if not self.segments and not self.cuts and len(self.clips) < 2:
            self.statusBar().showMessage("Add some parts first (S to split, I/O to mark a part).", 4000)
            return
        out_dir = self._output_dir()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, f"Can't use that output folder:\n\n{e}")
            return

        src = Path(self.path).resolve()
        sources = {os.path.normcase(str(Path(c.path).resolve())) for c in self.clips}
        # With no parts, export the whole (edited and/or joined) timeline as one file.
        parts = [(seg.start, seg.end, seg.name or self._default_name(i)) for i, seg in enumerate(self.segments)] \
            or [(0.0, self.duration, f"{src.stem} - {'edited' if self.cuts else 'joined'}")]
        jobs, seen = [], set()
        for i, (start, end, name) in enumerate(parts):
            out = (out_dir / (sanitize_filename(name) + src.suffix)).resolve()
            key = os.path.normcase(str(out))
            if key in sources:
                QMessageBox.critical(self, APP_NAME, f"Part {i + 1} would overwrite the source video. Rename it.")
                return
            if key in seen:
                QMessageBox.critical(self, APP_NAME, f"Two parts are both named '{out.name}'. Rename one.")
                return
            seen.add(key)
            pieces = [fp for a, b in self.cuts.kept(start, end) for fp in self._file_pieces(a, b)]
            jobs.append((pieces, sum(b - a for _, a, b in pieces), str(out)))

        existing = [Path(j[2]).name for j in jobs if Path(j[2]).exists()]
        if existing and QMessageBox.question(
                self, APP_NAME, "These files already exist and will be overwritten:\n\n" + "\n".join(existing[:15])
                + ("\n…" if len(existing) > 15 else "")) != QMessageBox.Yes:
            return
        needed = sum(j[1] for j in jobs) / self.duration * self.info["size"]
        free = shutil.disk_usage(out_dir).free
        if needed > free and QMessageBox.question(
                self, APP_NAME, f"The parts need about {fmt_size(needed)} but only {fmt_size(free)} is free. "
                                "Export anyway?") != QMessageBox.Yes:
            return

        self.exporter = Exporter(self.ffmpeg, jobs, self)
        self.exporter.progress.connect(self._on_export_progress)
        self.exporter.done.connect(self._on_export_done)
        self._export_clock = QElapsedTimer()
        self._export_clock.start()
        self.progress.setValue(0)
        self._refresh_all()
        self.exporter.start()

    def _on_export_progress(self, frac, index, count):
        self.progress.setValue(int(frac * 1000))
        elapsed = self._export_clock.elapsed() / 1000
        self.progress_label.setText(f"Part {index + 1} of {count} · {frac * 100:.0f}% · {fmt_time(elapsed, ms=False)}")

    def _on_export_done(self, ok, message):
        exporter, self.exporter = self.exporter, None
        if exporter:
            exporter.deleteLater()
        elapsed = self._export_clock.elapsed() / 1000
        self._refresh_all()
        if ok:
            self.progress_label.setText(f"✔ {message} ({fmt_time(elapsed, ms=False)})")
            self.statusBar().showMessage(message, 8000)
        else:
            self.progress_label.setText(message.splitlines()[0])
            if not message.startswith("Export cancelled"):
                QMessageBox.critical(self, APP_NAME, message)

    def cancel_export(self):
        if self.exporter:
            self.exporter.cancel()

    # -- misc
    def show_shortcuts(self):
        box = QMessageBox(self)
        box.setWindowTitle("Keyboard shortcuts")
        rows = "".join(
            f"<tr><td style='padding-right:18px'><b>{k}</b></td><td>{v}</td></tr>" if k else "<tr><td>&nbsp;</td></tr>"
            for k, _, v in (line.partition("\t") for line in SHORTCUTS_HELP.splitlines()))
        box.setText(f"<table>{rows}</table>")
        box.exec()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and any(u.isLocalFile() for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        files = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        files = [f for f in files if os.path.isfile(f)]
        if files:
            self.open_files(files, add=bool(self.clips))

    def closeEvent(self, e):
        if self.exporter:
            if QMessageBox.question(self, APP_NAME, "An export is running. Cancel it and quit?") != QMessageBox.Yes:
                e.ignore()
                return
            self.exporter.cancel()
            if self.exporter and self.exporter.proc:
                self.exporter.proc.waitForFinished(3000)
        self._stop_waveform()
        self.player.stop()
        e.accept()


def apply_dark_theme(app):
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(40, 40, 45))
    pal.setColor(QPalette.WindowText, QColor(225, 225, 230))
    pal.setColor(QPalette.Base, QColor(30, 30, 34))
    pal.setColor(QPalette.AlternateBase, QColor(38, 38, 43))
    pal.setColor(QPalette.ToolTipBase, QColor(50, 50, 56))
    pal.setColor(QPalette.ToolTipText, QColor(230, 230, 235))
    pal.setColor(QPalette.Text, QColor(225, 225, 230))
    pal.setColor(QPalette.Button, QColor(55, 55, 62))
    pal.setColor(QPalette.ButtonText, QColor(225, 225, 230))
    pal.setColor(QPalette.Highlight, QColor(70, 130, 220))
    pal.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    pal.setColor(QPalette.PlaceholderText, QColor(130, 130, 140))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        pal.setColor(QPalette.Disabled, role, QColor(120, 120, 128))
    app.setPalette(pal)


def main():
    if sys.platform == "win32":
        # Own taskbar identity, so Windows shows our icon instead of Python's.
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("VideoCutter.App")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(QIcon(str(Path(__file__).with_name("icon.ico"))))
    apply_dark_theme(app)
    win = MainWindow()
    win.show()
    files = [a for a in sys.argv[1:] if os.path.isfile(a)]
    if files:
        win.open_files(files)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
