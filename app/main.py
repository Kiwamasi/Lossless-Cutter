"""Video Cutter - a quick, lossless video cutter.

Plays the video with Qt, lets you mark parts on a timeline, and exports each
part with `ffmpeg -c copy` (no re-encode), so even huge files cut in seconds.
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QObject, QPointF, QProcess, QRectF, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QKeySequence, QPainter, QPalette, QPen, QPolygonF
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton, QSlider, QSplitter, QStyle, QTableWidget,
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
Delete\tRemove the selected part
Esc\tClear the in point / selection
Ctrl+Z\tUndo the last change to the parts
Ctrl+Y / Ctrl+Shift+Z\tRedo

Mouse wheel on timeline\tZoom
Shift + wheel\tScroll the timeline
Drag a part's edge\tAdjust it
Ctrl+0 / Ctrl+= / Ctrl+-\tZoom to fit / in / out

Ctrl+O\tOpen a video
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
         "format=duration,size:stream=codec_type,avg_frame_rate,width,height", "-of", "json", path],
        capture_output=True, text=True, creationflags=NO_WINDOW, timeout=120,
    )
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "ffprobe failed")
    data = json.loads(res.stdout)
    fmt = data.get("format", {})
    info = {
        "duration": float(fmt.get("duration") or 0),
        "size": int(fmt.get("size") or os.path.getsize(path)),
        "fps": 30.0, "width": 0, "height": 0,
    }
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            num, _, den = s.get("avg_frame_rate", "0/1").partition("/")
            try:
                if float(num) > 0 and float(den or 1) > 0:
                    info["fps"] = float(num) / float(den or 1)
            except ValueError:
                pass
            info["width"], info["height"] = s.get("width", 0), s.get("height", 0)
            break
    return info


def nearest_keyframe(ffprobe, path, t):
    """Time of the video keyframe closest to t, or None if none was found.

    Only reads packet headers in a small window around t, so it is fast even on huge files.
    """
    for window in (10, 60):
        res = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-read_intervals", f"{max(0.0, t - window):.3f}%{t + window:.3f}",
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
        if keys:
            return min(keys, key=lambda k: abs(k - t))
    return None


@dataclass(eq=False)
class Segment:
    start: float
    end: float
    name: str | None = None  # None = use the default "<file> - partN" name

    @property
    def length(self):
        return self.end - self.start


# ---------------------------------------------------------------- timeline

class Timeline(QWidget):
    seekRequested = Signal(float)
    selectionChanged = Signal(object)          # Segment or None
    edgeDragStarted = Signal()
    edgeDragged = Signal()
    edgeDragFinished = Signal(object, str)     # Segment, "start" | "end"

    MARGIN = 10
    RULER_H = 24
    EDGE_PX = 6
    TICK_STEPS = [0.1, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(100)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.duration = 0.0
        self.segments: list[Segment] = []
        self.selected: Segment | None = None
        self.position = 0.0
        self.in_point = None
        self.view_start = 0.0
        self.view_len = 0.0
        self._drag = None  # ("seek",) or ("edge", segment, which)

    # -- view
    def set_duration(self, d):
        self.duration = d
        self.view_start, self.view_len = 0.0, d
        self.update()

    def set_position(self, t):
        self.position = t
        if self._drag is None and self.view_len < self.duration:
            if not (self.view_start <= t <= self.view_start + self.view_len):
                self.view_start = self._clamp_view(t - self.view_len * 0.1)
        self.update()

    def zoom(self, factor, anchor=None):
        if self.duration <= 0:
            return
        anchor = self.position if anchor is None else anchor
        frac = (anchor - self.view_start) / self.view_len if self.view_len else 0.5
        self.view_len = min(self.duration, max(2.0, self.view_len * factor))
        self.view_start = self._clamp_view(anchor - frac * self.view_len)
        self.update()

    def zoom_fit(self):
        self.view_start, self.view_len = 0.0, self.duration
        self.update()

    def _clamp_view(self, vs):
        return min(max(0.0, vs), max(0.0, self.duration - self.view_len))

    def _plot_w(self):
        return max(1, self.width() - 2 * self.MARGIN)

    def x_for(self, t):
        if self.view_len <= 0:
            return float(self.MARGIN)
        return self.MARGIN + (t - self.view_start) / self.view_len * self._plot_w()

    def t_for(self, x):
        if self.view_len <= 0:
            return 0.0
        t = self.view_start + (x - self.MARGIN) / self._plot_w() * self.view_len
        return min(max(0.0, t), self.duration)

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
            x = self.x_for(t)
            if self.MARGIN - 1 <= x <= w - self.MARGIN + 1:
                p.setPen(QColor(110, 110, 120))
                p.drawLine(QPointF(x, self.RULER_H - 7), QPointF(x, self.RULER_H))
                p.setPen(QColor(175, 175, 185))
                label = f"{t:.1f}s" if step < 1 else fmt_time(t, ms=False)
                p.drawText(QPointF(x + 3, self.RULER_H - 9), label)

        top, bot = self.RULER_H + 8, h - 10
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
                           f"Part {i + 1}\n{fmt_time(seg.length, ms=False)}")

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
            seg = self._segment_at(self.t_for(x))
            if seg is not self.selected:
                self.selected = seg
                self.selectionChanged.emit(seg)
        self._drag = ("seek",)
        self.seekRequested.emit(self.t_for(x))
        self.update()

    def mouseMoveEvent(self, e):
        x = e.position().x()
        if self._drag is None:
            if e.position().y() > self.RULER_H and self._hit_edge(x):
                self.setCursor(Qt.SizeHorCursor)
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
    """Runs one ffmpeg stream-copy job per part, one after another."""
    progress = Signal(float, int, int)  # overall fraction, current job index, job count
    done = Signal(bool, str)

    def __init__(self, ffmpeg, src, jobs, parent=None):
        super().__init__(parent)
        self.ffmpeg, self.src, self.jobs = ffmpeg, src, jobs  # jobs: [(start, length, out_path)]
        self.total = sum(j[1] for j in jobs) or 1.0
        self.index = 0
        self.done_len = 0.0
        self.cancelled = False
        self.proc = None
        self._buf = ""

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
        start, length, out = self.jobs[self.index]
        self.progress.emit(self.done_len / self.total, self.index, len(self.jobs))
        self._buf = ""
        self.proc = QProcess(self)
        self.proc.setProgram(self.ffmpeg)
        self.proc.setArguments([
            "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-ss", f"{start:.6f}", "-i", self.src, "-t", f"{length:.6f}",
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

        self.path = None
        self.info = {}
        self.duration = 0.0
        self.segments: list[Segment] = []
        self.pos = 0.0
        self.exporter = None
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

        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(8, 8, 8, 0)
        top_layout.addWidget(self.video, 1)
        top_layout.addLayout(controls)
        top_layout.addWidget(self.timeline)

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
        table_header.addWidget(self._button("Delete part", self.delete_selected, "Delete the selected part (Delete)"))
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
        bottom_layout.addLayout(table_box, 1)
        bottom_layout.addWidget(panel_widget)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(top)
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([620, 260])
        self.setCentralWidget(splitter)

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

        act(menu_file, "&Open video…", "Ctrl+O", self.open_dialog)
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
        act(menu_edit, "Delete selected part", "Delete", self.delete_selected)
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
        path, _ = QFileDialog.getOpenFileName(self, "Open video", self.settings.value("last_dir", ""),
                                              f"Video files ({VIDEO_EXTS});;All files (*)")
        if path:
            self.load(path)

    def load(self, path):
        if self.exporter:
            QMessageBox.information(self, APP_NAME, "Wait for the export to finish first.")
            return
        if not self._check_tools():
            return
        if self.segments and QMessageBox.question(
                self, APP_NAME, "Discard the current parts and open a new video?") != QMessageBox.Yes:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            info = probe(self.ffprobe, path)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, APP_NAME, f"Could not read this file:\n\n{e}")
            return
        QApplication.restoreOverrideCursor()
        if info["duration"] <= 0:
            QMessageBox.critical(self, APP_NAME, "Could not determine the video's duration.")
            return

        self.path, self.info, self.duration = path, info, info["duration"]
        self.segments.clear()
        self._undo.clear()
        self._redo.clear()
        self.timeline.selected = None
        self.timeline.in_point = None
        self.timeline.set_duration(self.duration)
        self.out_edit.setText(str(Path(path).parent))
        self.settings.setValue("last_dir", str(Path(path).parent))
        self.setWindowTitle(f"{Path(path).name} — {APP_NAME}")
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.pause()  # shows the first frame
        self.pos = 0.0
        self._refresh_all()
        self.statusBar().showMessage("Loaded. Press S to split at the playhead, or I / O to mark a part.", 8000)

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
        self.pos = min(max(0.0, t), self.duration)
        self.timeline.set_position(self.pos)
        self._update_time_label()
        self._pending_seek = self.pos
        if not self._seek_timer.isActive():
            self._seek_timer.start()

    def _apply_seek(self):
        if self._pending_seek is not None:
            self.player.setPosition(int(round(self._pending_seek * 1000)))
            self._pending_seek = None

    def _on_player_position(self, ms):
        # Ignore stale positions while a seek is pending or the user is dragging.
        if self._seek_timer.isActive() or self.timeline._drag is not None:
            return
        self.pos = ms / 1000
        self.timeline.set_position(self.pos)
        self._update_time_label()

    def nudge(self, seconds):
        self.seek(self.pos + seconds)

    def step_frames(self, n):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        self.seek(self.pos + n / self.info.get("fps", 30.0))

    def jump_cut(self, direction):
        points = sorted({0.0, self.duration, *(s.start for s in self.segments), *(s.end for s in self.segments)}
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
                                        text=fmt_time(self.pos))
        if ok:
            t = parse_time(text)
            if t is None:
                self.statusBar().showMessage("Couldn't read that time.", 4000)
            else:
                self.seek(t)

    def _update_time_label(self):
        self.time_label.setText(f"{fmt_time(self.pos)} / {fmt_time(self.duration)}")

    # -- undo / redo
    def _snapshot(self):
        sel = self.timeline.selected
        return ([(s.start, s.end, s.name) for s in self.segments], self.timeline.in_point,
                self.segments.index(sel) if sel in self.segments else -1)

    def _push_undo(self):
        """Call right before changing the parts or the in point."""
        self._undo.append(self._snapshot())
        del self._undo[:-200]
        self._redo_before_push, self._redo = self._redo, []

    def _drop_noop_undo(self):
        """Undo the last _push_undo if nothing actually changed since."""
        if self._undo and self._undo[-1][:2] == self._snapshot()[:2]:
            self._undo.pop()
            self._redo = self._redo_before_push

    def _restore(self, snap):
        parts, in_point, sel = snap
        self.segments[:] = [Segment(*p) for p in parts]
        self.timeline.in_point = in_point
        self.timeline.selected = self.segments[sel] if 0 <= sel < len(self.segments) else None
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
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            kf = nearest_keyframe(self.ffprobe, self.path, t)
        except Exception:
            kf = None
        finally:
            QApplication.restoreOverrideCursor()
        return t if kf is None else min(max(0.0, kf), self.duration)

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
        cuts = [0.0] + [self.snap(self.duration * k / n) for k in range(1, n)] + [self.duration]
        cuts = sorted(set(cuts))
        self._push_undo()
        self.segments.clear()
        self.segments.extend(Segment(a, b) for a, b in zip(cuts, cuts[1:]) if b - a > 0.05)
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
            est = seg.length / self.duration * size if self.duration else 0
            values = [str(i + 1), seg.name or self._default_name(i), fmt_time(seg.start), fmt_time(seg.end),
                      fmt_time(seg.length), "~" + fmt_size(est)]
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
                t = self.snap(min(t, self.duration))
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
        total = sum(s.length for s in self.segments)
        self.parts_label.setText(f"<b>Parts: {n}</b>  ·  total {fmt_time(total, ms=False)}" if n else
                                 "<b>Parts</b> — press <b>S</b> to split at the playhead, or <b>I</b>/<b>O</b> to mark a part")
        busy = self.exporter is not None
        self.export_btn.setText(f"Export {n} part{'s' if n != 1 else ''}" if n else "Export")
        self.export_btn.setEnabled(bool(n) and not busy)
        self.cancel_btn.setVisible(busy)
        self.progress.setVisible(busy)
        if self.path:
            i = self.info
            self.file_label.setText(
                f"<b>{Path(self.path).name}</b><br>{i['width']}×{i['height']} · {i['fps']:.3g} fps · "
                f"{fmt_size(i['size'])} · {fmt_time(self.duration, ms=False)}")

    # -- export
    def export(self):
        if not self._has_media() or self.exporter:
            return
        if not self.segments:
            self.statusBar().showMessage("Add some parts first (S to split, I/O to mark a part).", 4000)
            return
        out_dir = self._output_dir()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, f"Can't use that output folder:\n\n{e}")
            return

        src = Path(self.path).resolve()
        jobs, seen = [], set()
        for i, seg in enumerate(self.segments):
            out = (out_dir / (sanitize_filename(seg.name or self._default_name(i)) + src.suffix)).resolve()
            key = os.path.normcase(str(out))
            if key == os.path.normcase(str(src)):
                QMessageBox.critical(self, APP_NAME, f"Part {i + 1} would overwrite the source video. Rename it.")
                return
            if key in seen:
                QMessageBox.critical(self, APP_NAME, f"Two parts are both named '{out.name}'. Rename one.")
                return
            seen.add(key)
            jobs.append((seg.start, seg.length, str(out)))

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

        self.exporter = Exporter(self.ffmpeg, str(src), jobs, self)
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
        if files:
            self.load(files[0])

    def closeEvent(self, e):
        if self.exporter:
            if QMessageBox.question(self, APP_NAME, "An export is running. Cancel it and quit?") != QMessageBox.Yes:
                e.ignore()
                return
            self.exporter.cancel()
            if self.exporter and self.exporter.proc:
                self.exporter.proc.waitForFinished(3000)
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
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        win.load(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
