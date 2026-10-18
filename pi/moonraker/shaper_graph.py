#!/usr/bin/env python3
# Draws a SHAPER_CALIBRATE result (its /tmp/calibration_data_<axis>_<name>.csv)
# as a PNG, like Klipper's calibrate_shaper.py but with Pillow only: the
# printer's Python has no matplotlib. Run by CALIBRATE_INPUT_SHAPER_GRAPH.
import argparse
import csv
import os
import sys

from PIL import Image, ImageDraw, ImageFont

W, H = 1400, 820
LEFT, RIGHT, TOP, BOTTOM = 100, 90, 70, 190
PSD = [('psd_x', 'X', (214, 39, 40)), ('psd_y', 'Y', (44, 160, 44)),
       ('psd_z', 'Z', (31, 119, 180)), ('psd_xyz', 'X+Y+Z', (20, 20, 20))]
SHAPER_COLOURS = [(148, 103, 189), (255, 127, 14), (23, 190, 207),
                  (227, 119, 194), (140, 86, 75), (188, 189, 34)]


def font(size):
    # Scalable fonts need Pillow's FreeType support, which the printer's
    # Pillow lacks: then (size, None) and text() scales the bitmap font.
    for path in ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                 '/usr/share/fonts/TTF/DejaVuSans.ttf'):
        try:
            return (size, ImageFont.truetype(path, size))
        except (OSError, ImportError):
            pass
    try:
        return (size, ImageFont.load_default(size=size))
    except (TypeError, OSError, ImportError):
        return (size, None)


def text(img, d, xy, s, font, anchor='la'):
    # anchor: first letter l/m/r (left, middle, right of xy), second a/m/t
    # (xy is the top, middle or top of the text, as in Pillow).
    size, face = font
    if face is not None:
        d.text(xy, s, fill='black', font=face, anchor=anchor)
        return
    bitmap = ImageFont.load_default()
    box = d.textbbox((0, 0), s, font=bitmap)
    w, h = box[2] - box[0], box[3] - box[1]
    scale = max(1, round(size / 11.))
    tile = Image.new('L', (w + 2, h + 2), 0)
    ImageDraw.Draw(tile).text((-box[0] + 1, -box[1] + 1), s, fill=255,
                              font=bitmap)
    tile = tile.resize((tile.width * scale, tile.height * scale))
    x, y = xy
    x -= {'l': 0, 'm': tile.width / 2, 'r': tile.width}[anchor[0]]
    y -= {'a': 0, 't': 0, 'm': tile.height / 2}[anchor[1]]
    img.paste((0, 0, 0), (int(x), int(y)), tile)


def read(path):
    with open(path) as f:
        rows = list(csv.reader(f))
    header, data = rows[0], [r for r in rows[1:] if r]
    cols = {name: [float(r[i]) for r in data] for i, name in enumerate(header)}
    shapers = [name for name in header[6:]]
    return cols, shapers


def dashed(draw, points, fill, width, dash=10):
    on = True
    left = dash
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** .5
        pos = 0.
        while pos < length:
            step = min(left, length - pos)
            if on:
                a, b = pos / length, (pos + step) / length
                draw.line([(x0 + (x1 - x0) * a, y0 + (y1 - y0) * a),
                           (x0 + (x1 - x0) * b, y0 + (y1 - y0) * b)],
                          fill=fill, width=width)
            pos += step
            left -= step
            if left <= 0:
                on, left = not on, dash


def main():
    p = argparse.ArgumentParser()
    p.add_argument('csv')
    p.add_argument('--axis', default='')
    p.add_argument('--shaper', default='')
    p.add_argument('--freq', type=float, default=0.)
    p.add_argument('--out-dir', required=True)
    a = p.parse_args()
    if not os.path.exists(a.csv):
        sys.exit('No calibration data at %s; run SHAPER_CALIBRATE NAME=oc first'
                 % a.csv)
    cols, shapers = read(a.csv)
    freqs = cols.get('freq') or sys.exit('%s has no data' % a.csv)
    fmax = freqs[-1]
    pmax = max(max(cols[key]) for key, _, _ in PSD) * 1.05 or 1.
    x0, x1, y0, y1 = LEFT, W - RIGHT, TOP, H - BOTTOM

    def px(f):
        return x0 + (x1 - x0) * f / fmax

    def py(v, top):
        return y1 - (y1 - y0) * v / top

    img = Image.new('RGB', (W, H), 'white')
    d = ImageDraw.Draw(img)
    small, normal, big = font(15), font(18), font(24)

    # Grid and axes
    for f in range(0, int(fmax) + 1, 10):
        d.line([(px(f), y0), (px(f), y1)], fill=(232, 232, 232))
        if f % 20 == 0:
            text(img, d, (px(f), y1 + 8), str(f), small, 'mt')
    for i in range(6):
        y = y0 + (y1 - y0) * i / 5
        d.line([(x0, y), (x1, y)], fill=(232, 232, 232))
        text(img, d, (x0 - 8, y), '%.1e' % (pmax * (5 - i) / 5), small, 'rm')
        text(img, d, (x1 + 8, y), '%.1f' % ((5 - i) / 5), small, 'lm')
    d.rectangle([x0, y0, x1, y1], outline='black')
    text(img, d, ((x0 + x1) / 2, y1 + 32), 'Frequency, Hz', normal, 'mt')
    text(img, d, (x0, y0 - 24), 'Power spectral density', small)
    text(img, d, (x1, y0 - 24), 'Shaper response', small, 'ra')
    title = 'Input shaper, %s axis' % a.axis.upper() if a.axis else 'Input shaper'
    if a.shaper:
        title += ': recommended %s @ %.1f Hz' % (a.shaper, a.freq)
    text(img, d, (x0, 14), title, big)

    legend = []
    # Shaper responses (right axis, 0..1), the recommended one solid
    chosen = None
    for i, name in enumerate(shapers):
        colour = SHAPER_COLOURS[i % len(SHAPER_COLOURS)]
        pts = [(px(f), py(v, 1.)) for f, v in zip(freqs, cols[name])]
        if a.shaper and name.split('(')[0] == a.shaper:
            chosen = name
            d.line(pts, fill=colour, width=4)
        else:
            dashed(d, pts, colour, 2)
        # "mzv(53.8)" -> "mzv (53.8 Hz)"
        legend.append((name.replace('(', ' (').replace(')', ' Hz)'), colour,
                       name == chosen))
    # Measured vibrations (left axis)
    for key, label, colour in PSD:
        pts = [(px(f), py(v, pmax)) for f, v in zip(freqs, cols[key])]
        d.line(pts, fill=colour, width=3 if key == 'psd_xyz' else 2)
        legend.insert(0, (label, colour, key == 'psd_xyz'))
    # What is left after the recommended shaper
    if chosen:
        after = [s * v for s, v in zip(cols['psd_xyz'], cols[chosen])]
        dashed(d, [(px(f), py(v, pmax)) for f, v in zip(freqs, after)],
               (90, 90, 90), 3, dash=4)
        legend.insert(4, ('After %s' % a.shaper, (90, 90, 90), True))

    # Legend in columns under the chart
    lx, ly = x0, y1 + 70
    for label, colour, bold in legend:
        d.line([(lx, ly + 9), (lx + 34, ly + 9)], fill=colour,
               width=4 if bold else 2)
        text(img, d, (lx + 42, ly), label, small)
        lx += 230
        if lx > x1 - 200:
            lx, ly = x0, ly + 28

    os.makedirs(a.out_dir, exist_ok=True)
    out = os.path.join(a.out_dir, 'shaper_%s.png' % (a.axis or 'graph'))
    img.save(out)
    print('Graph saved to %s' % out)


if __name__ == '__main__':
    main()
