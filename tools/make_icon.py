"""Draws the app icon and writes app/icon.ico (multi-size) and app/icon.png.

Run again after tweaking the drawing:  python tools/make_icon.py
"""
import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF

SIZES = [16, 24, 32, 48, 64, 128, 256]


def draw(size):
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 256, size / 256)  # draw on a 256x256 canvas

    # rounded background
    grad = QLinearGradient(0, 0, 0, 256)
    grad.setColorAt(0, QColor(58, 64, 82))
    grad.setColorAt(1, QColor(28, 30, 38))
    bg = QPainterPath()
    bg.addRoundedRect(QRectF(8, 8, 240, 240), 52, 52)
    p.fillPath(bg, grad)

    # play triangle
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(245, 245, 250))
    p.drawPolygon(QPolygonF([QPointF(92, 50), QPointF(92, 150), QPointF(178, 100)]))

    # timeline cut into three parts (the app's part colours)
    y, h = 178, 38
    for x, w, c in ((34, 72, QColor(70, 130, 220)), (114, 52, QColor(225, 140, 50)), (174, 48, QColor(80, 180, 110))):
        p.setBrush(c)
        p.drawRoundedRect(QRectF(x, y, w, h), 8, 8)

    # red playhead on one of the cuts
    p.setPen(QPen(QColor(240, 60, 60), 8, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(110, 166), QPointF(110, 228))
    p.end()
    return img


def png_bytes(img):
    buf = QBuffer()
    buf.open(QBuffer.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def write_ico(path, images):
    pngs = [png_bytes(i) for i in images]
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries = b""
    for img, data in zip(images, pngs):
        dim = img.width() if img.width() < 256 else 0  # 0 means 256 in ICO
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    Path(path).write_bytes(header + entries + b"".join(pngs))


if __name__ == "__main__":
    app = QGuiApplication(sys.argv)
    out = Path(__file__).resolve().parent.parent / "app"
    out.mkdir(exist_ok=True)
    write_ico(out / "icon.ico", [draw(s) for s in SIZES])
    draw(256).save(str(out / "icon.png"))
    print(f"wrote {out / 'icon.ico'} and icon.png")
