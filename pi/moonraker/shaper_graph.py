#!/usr/bin/env python3
# Draws a SHAPER_CALIBRATE result (its /tmp/calibration_data_<axis>_<name>.csv)
import argparse
import csv
import glob
import math
import os
import sys
import textwrap

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    for site in (glob.glob('/usr/prog/moonraker/moonraker-env/lib/python3*/site-packages')
                 + glob.glob(os.path.expanduser('~/moonraker-env/lib/python3*/site-packages'))):
        sys.path.append(site)
    from PIL import Image, ImageDraw, ImageFont

W, H = 1400, 1000
LEFT, RIGHT, TOP, BOTTOM = 130, 120, 110, 90
GREY, LIGHT = (128, 128, 128), (211, 211, 211)
# Klipper's colours: matplotlib names for the measured axes, then
# matplotlib's default cycle for the shapers.
PSD = [('psd_xyz', 'X+Y+Z', (128, 0, 128)), ('psd_x', 'X', (255, 0, 0)),
       ('psd_y', 'Y', (0, 128, 0)), ('psd_z', 'Z', (0, 0, 255))]
AFTER = (0, 255, 255)
CYCLE = [(31, 119, 180), (255, 127, 14), (44, 160, 44), (214, 39, 40),
         (148, 103, 189), (140, 86, 75)]
MAX_TITLE_LENGTH = 65


def font(size):
    # Scalable fonts need Pillow's FreeType support, which the printer's
    # Pillow lacks: then (size, None) and the bitmap font is scaled up.
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


def text_tile(s, font):
    # The text as a white-on-black mask, for pasting in black.
    size, face = font
    if face is None:
        face, scale = ImageFont.load_default(), max(1, round(size / 11.))
    else:
        scale = 1
    probe = ImageDraw.Draw(Image.new('L', (1, 1)))
    box = probe.textbbox((0, 0), s, font=face)
    tile = Image.new('L', (box[2] - box[0] + 2, box[3] - box[1] + 2), 0)
    ImageDraw.Draw(tile).text((1 - box[0], 1 - box[1]), s, fill=255, font=face)
    if scale > 1:
        tile = tile.resize((tile.width * scale, tile.height * scale))
    return tile


def text(img, xy, s, font, anchor='lt', rotate=0):
    # anchor: l/m/r then t/m/b, the point of the text that lands on xy.
    tile = text_tile(s, font)
    if rotate:
        tile = tile.rotate(rotate, expand=True)
    x, y = xy
    x -= {'l': 0, 'm': tile.width / 2, 'r': tile.width}[anchor[0]]
    y -= {'t': 0, 'm': tile.height / 2, 'b': tile.height}[anchor[1]]
    img.paste((0, 0, 0), (int(x), int(y)), tile)


def styled_line(d, pts, fill, width, style):
    # matplotlib's solid, dotted and dashdot lines.
    pattern = {'dotted': [width, width * 1.65],
               'dashdot': [width * 6.4, width * 1.6, width, width * 1.6]}.get(style)
    if pattern is None:
        d.line(pts, fill=fill, width=width, joint='curve')
        return
    i, left, on = 0, pattern[0], True
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        length = math.hypot(x1 - x0, y1 - y0)
        pos = 0.
        while pos < length:
            step = min(left, length - pos)
            if on:
                a, b = pos / length, (pos + step) / length
                d.line([(x0 + (x1 - x0) * a, y0 + (y1 - y0) * a),
                        (x0 + (x1 - x0) * b, y0 + (y1 - y0) * b)],
                       fill=fill, width=width)
            pos += step
            left -= step
            if left <= 1e-9:
                i = (i + 1) % len(pattern)
                left, on = pattern[i], i % 2 == 0


def nice_ticks(top):
    # matplotlib-like major ticks for 0..top, and the minor step.
    raw = top / 6.
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    ticks = [step * i for i in range(int(math.ceil(top / step - 1e-9)) + 1)]
    mantissa = step / 10 ** math.floor(math.log10(step))
    minor = step / (5 if round(mantissa, 6) in (1, 5, 10) else 4)
    return ticks, minor


def read(path):
    with open(path) as f:
        rows = list(csv.reader(f))
    header, data = rows[0], [r for r in rows[1:] if r]
    cols = {name: [float(r[i]) for r in data] for i, name in enumerate(header)}
    return cols, header[6:]


def shaper_stats(klippy, cols, names, scv):
    # Klipper's legend figures for each shaper, from Kalico's own code: the
    # worst remaining vibrations over its test damping ratios (as its fit
    # does), smoothing and the max accel for the square corner velocity.
    sys.path.insert(0, klippy)
    import numpy as np
    from extras import shaper_calibrate, shaper_defs
    helper = shaper_calibrate.ShaperCalibrate(printer=None)
    cfgs = {c.name: c for c in shaper_defs.INPUT_SHAPERS}
    freqs = np.array(cols['freq'])
    psd = np.array(cols['psd_xyz'])
    stats = {}
    for name in names:
        kind, freq = name.split('(')[0], float(name.split('(')[1].rstrip(')'))
        if kind not in cfgs:
            continue
        shaper = cfgs[kind].init_func(freq, shaper_defs.DEFAULT_DAMPING_RATIO)
        vibrs = max(helper._estimate_remaining_vibrations(shaper, dr, freqs, psd)[0]
                    for dr in shaper_calibrate.TEST_DAMPING_RATIOS)
        stats[name] = (kind, freq, vibrs,
                       helper._get_shaper_smoothing(shaper, scv=scv),
                       helper.find_shaper_max_accel(shaper, scv))
    return stats


def legend(img, d, corner, entries, small, x0, y0, x1):
    # entries: (label, colour or None, style); a framed box like matplotlib's.
    pad, line_w, gap = 10, 44, 8
    tiles = [text_tile(label, small) for label, _, _ in entries]
    row_h = [max(t.height, 14) + 6 for t in tiles]
    width = pad * 2 + line_w + gap + max(t.width for t in tiles)
    height = pad * 2 + sum(row_h)
    bx = x0 + 10 if corner == 'left' else x1 - 10 - width
    by = y0 + 10
    d.rounded_rectangle([bx, by, bx + width, by + height], radius=6,
                        fill=(255, 255, 255), outline=LIGHT)
    y = by + pad
    for (label, colour, style), tile, h in zip(entries, tiles, row_h):
        if colour is not None:
            styled_line(d, [(bx + pad, y + h / 2), (bx + pad + line_w, y + h / 2)],
                        colour, 3, style)
        img.paste((0, 0, 0), (int(bx + pad + line_w + gap), int(y + (h - tile.height) / 2)), tile)
        y += h


def main():
    p = argparse.ArgumentParser()
    p.add_argument('csv')
    p.add_argument('--axis', default='')
    p.add_argument('--shaper', default='')
    p.add_argument('--freq', type=float, default=0.)
    p.add_argument('--scv', type=float, default=5.)
    p.add_argument('--klippy', default='/usr/data/opencreator/klipper/klipper/klippy')
    p.add_argument('--out-dir', required=True)
    a = p.parse_args()
    if not os.path.exists(a.csv):
        sys.exit('No calibration data at %s; run SHAPER_CALIBRATE NAME=oc first'
                 % a.csv)
    cols, names = read(a.csv)
    freqs = cols.get('freq') or sys.exit('%s has no data' % a.csv)
    try:
        stats = shaper_stats(a.klippy, cols, names, a.scv)
    except Exception as e:
        print('Shaper figures unavailable (%s); graph without them' % e)
        stats = {}

    fmax = freqs[-1]
    pmax = max(max(cols[key]) for key, _, _ in PSD) or 1.
    yticks, yminor = nice_ticks(pmax)
    ytop = yticks[-1]
    x0, x1, y0, y1 = LEFT, W - RIGHT, TOP, H - BOTTOM

    def px(f):
        return x0 + (x1 - x0) * f / fmax

    def py(v, top):
        return y1 - (y1 - y0) * v / top

    img = Image.new('RGB', (W, H), 'white')
    d = ImageDraw.Draw(img)
    small, normal, big = font(15), font(19), font(21)

    # Grid: minor every 5 Hz and between the major ticks, then major
    for f in range(0, int(fmax) + 1, 5):
        d.line([(px(f), y0), (px(f), y1)], fill=LIGHT)
    v = 0.
    while v <= ytop + 1e-9:
        d.line([(x0, py(v, ytop)), (x1, py(v, ytop))], fill=LIGHT)
        v += yminor
    for f in range(0, int(fmax) + 1, 25):
        d.line([(px(f), y0), (px(f), y1)], fill=GREY)
        text(img, (px(f), y1 + 8), str(f), small, 'mt')
    exp = int(math.floor(math.log10(ytop))) if ytop >= 10 else 0
    for v in yticks:
        d.line([(x0, py(v, ytop)), (x1, py(v, ytop))], fill=GREY)
        text(img, (x0 - 8, py(v, ytop)), ('%.1f' % (v / 10 ** exp)) if exp else '%g' % v,
             small, 'rm')
    for i in range(6):
        text(img, (x1 + 8, py(i / 5, 1.)), '%.1f' % (i / 5), small, 'lm')
    if exp:
        # matplotlib's scientific offset, printed once above the axis
        text(img, (x0, y0 - 6), '1e%d' % exp, small, 'lb')
    d.rectangle([x0, y0, x1, y1], outline='black')

    # Labels and title
    text(img, ((x0 + x1) / 2, y1 + 36), 'Frequency, Hz', normal, 'mt')
    text(img, (28, (y0 + y1) / 2), 'Power spectral density', normal, 'lm', 90)
    text(img, (W - 26, (y0 + y1) / 2), 'Shaper vibration reduction (ratio)',
         normal, 'rm', 90)
    title = 'Frequency response and shapers (%s)' % os.path.basename(a.csv)
    lines = textwrap.wrap(title, MAX_TITLE_LENGTH)
    for i, line in enumerate(lines):
        text(img, (W / 2, y0 - 26 - 28 * (len(lines) - 1 - i)), line, big, 'mb')

    # Shapers (right axis): dotted, the recommended one dash-dot
    selected = a.shaper.lower()
    right, chosen = [], None
    for i, name in enumerate(names):
        colour = CYCLE[i % len(CYCLE)]
        kind = name.split('(')[0]
        style = 'dashdot' if kind == selected else 'dotted'
        if kind == selected:
            chosen = name
        styled_line(d, [(px(f), py(v, 1.)) for f, v in zip(freqs, cols[name])],
                    colour, 3, style)
        if name in stats:
            k, freq, vibrs, sm, accel = stats[name]
            label = '%s (%.1f Hz, vibr=%.1f%%, sm~=%.2f, accel<=%.f)' % (
                k.upper(), freq, vibrs * 100., sm, round(accel / 100.) * 100.)
        else:
            label = '%s (%s Hz)' % (kind.upper(), name.split('(')[1].rstrip(')'))
        right.append((label, colour, style))
    if selected:
        right.append(('Recommended shaper: %s' % selected.upper(), None, ''))

    # Measured vibrations (left axis), then what the chosen shaper leaves
    left = []
    for key, label, colour in PSD:
        styled_line(d, [(px(f), py(v, ytop)) for f, v in zip(freqs, cols[key])],
                    colour, 3, 'solid')
        left.append((label, colour, 'solid'))
    if chosen:
        after = [s * v for s, v in zip(cols['psd_xyz'], cols[chosen])]
        styled_line(d, [(px(f), py(v, ytop)) for f, v in zip(freqs, after)],
                    AFTER, 3, 'solid')
        left.append(('After shaper', AFTER, 'solid'))

    legend(img, d, 'left', left, small, x0, y0, x1)
    legend(img, d, 'right', right, small, x0, y0, x1)

    os.makedirs(a.out_dir, exist_ok=True)
    out = os.path.join(a.out_dir, 'shaper_%s.png' % (a.axis or 'graph'))
    img.save(out)
    print('Graph saved to %s' % out)


if __name__ == '__main__':
    main()
