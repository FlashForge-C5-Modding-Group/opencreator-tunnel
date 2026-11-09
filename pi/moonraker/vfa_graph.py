#!/usr/bin/env python3
# Draws a VFA_COMPENSATION_GRAPH result for one motor, for people rather than
# engineers: how hard the toolhead shakes across print speeds before and after
# compensation, and how much each ripple was reduced. Reads what the stepper
# resonance tester left in /tmp (vfa_<stepper>_curve_before/after.csv and .info,
# vfa_<stepper>_summary.csv). Pillow draws it, with shaper_graph.py's helpers.
import argparse
import csv
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shaper_graph import (Image, ImageDraw, font, text, text_tile, styled_line,
                          nice_ticks, GREY, LIGHT)

W, H = 1400, 1060
LEFT, RIGHT = 130, 60
COLOURS = {1: (31, 119, 180), 2: (255, 127, 14), 4: (44, 160, 44)}
BEFORE_BAR = (190, 190, 190)
NAMES = {1: '1x ripple', 2: '2x ripple', 4: '4x ripple'}
MOTORS = {'stepper_x': 'X motor (stepper_x)', 'stepper_y': 'Y motor (stepper_y)'}


def read_csv(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        rows = [r for r in csv.reader(f) if r]
    if len(rows) < 2:
        return None
    header = rows[0]
    return [{k: float(v) for k, v in zip(header, r)} for r in rows[1:]]


def read_info(path):
    info = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                if '=' in line:
                    k, v = line.strip().split('=', 1)
                    info[k] = float(v)
    return info


def harmonics(curve):
    return sorted(int(k[2:-4]) for k in curve[0] if k.startswith('td') and k.endswith('_amp'))


def rms(values):
    return math.sqrt(sum(v * v for v in values) / len(values)) if values else 0.


def axes(img, d, box, xmax, ymax, small, xlabel, ylabel, xstep=None):
    # Grid, ticks and labels for a 0..xmax by 0..ymax panel; returns the mappers
    x0, y0, x1, y1 = box
    yticks, _ = nice_ticks(ymax)
    ytop = yticks[-1]
    xticks, _ = nice_ticks(xmax) if xstep is None else (
        [xstep * i for i in range(int(xmax / xstep) + 1)], None)
    xtop = xticks[-1]

    def px(v):
        return x0 + (x1 - x0) * v / xtop

    def py(v):
        return y1 - (y1 - y0) * v / ytop

    for v in yticks:
        d.line([(x0, py(v)), (x1, py(v))], fill=LIGHT)
        text(img, (x0 - 8, py(v)), '%g' % v, small, 'rm')
    for v in xticks:
        d.line([(px(v), y0), (px(v), y1)], fill=LIGHT)
        text(img, (px(v), y1 + 8), '%g' % v, small, 'mt')
    d.rectangle([x0, y0, x1, y1], outline='black')
    text(img, ((x0 + x1) / 2, y1 + 34), xlabel, small, 'mt')
    text(img, (x0 - 70, (y0 + y1) / 2), ylabel, small, 'mm', 90)
    return px, py


def box_legend(img, d, entries, small, right, top):
    # entries: (label, colour or None, style); framed, at the top right
    pad, line_w, gap = 10, 44, 8
    tiles = [text_tile(label, small) for label, _, _ in entries]
    row_h = [max(t.height, 14) + 6 for t in tiles]
    width = pad * 2 + line_w + gap + max(t.width for t in tiles)
    bx, by = right - 10 - width, top + 10
    d.rounded_rectangle([bx, by, bx + width, by + pad * 2 + sum(row_h)], radius=6,
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
    p.add_argument('stepper')
    p.add_argument('--data-dir', default='/tmp')
    p.add_argument('--out-dir', required=True)
    a = p.parse_args()
    base = os.path.join(a.data_dir, 'vfa_%s_' % a.stepper)
    before = read_csv(base + 'curve_before.csv')
    after = read_csv(base + 'curve_after.csv')
    summary = read_csv(base + 'summary.csv') or []
    info = read_info(base + 'curve_before.info')
    if before is None and not summary:
        sys.exit('No VFA results for %s in %s; run VFA_COMPENSATION_GRAPH first'
                 % (a.stepper, a.data_dir))

    img = Image.new('RGB', (W, H), 'white')
    d = ImageDraw.Draw(img)
    small, normal, big = font(15), font(18), font(24)
    x0, x1 = LEFT, W - RIGHT

    # Title: the motor and its resonance. What it all means is in the docs.
    # The frequency it was tuned at (with two resonances, the one kept), not
    # just the strongest peak in the sweep
    res = summary[0]['freq'] if summary else (info.get('res_freq') if info.get('trusted') else None)
    title = 'VFA compensation: %s' % MOTORS.get(a.stepper, a.stepper)
    if res:
        title += '  ·  tuned at %.0f Hz' % res
    text(img, (W / 2, 28), title, big, 'mt')
    y = 60

    # Panel 1: shaking across speeds
    top1 = y + 40
    box1 = (x0, top1, x1, top1 + 470)
    text(img, (x0, top1 - 12), 'Shaking at each print speed (lower is better)', normal, 'lb')
    if before is not None:
        tdxs = harmonics(before)
        curves = [c for c in (before, after) if c]
        vmax = max(r['vel'] for c in curves for r in c)
        amax = max([r['td%d_amp' % t] for c in curves for r in c for t in tdxs
                    if not math.isnan(r['td%d_amp' % t])] or [1.])
        px, py = axes(img, d, box1, vmax, amax * 1.1, small, 'Print speed, mm/s',
                      'Shaking (accelerometer, mm/s²)', xstep=10 if vmax <= 150 else 20)
        # Where each ripple was tuned
        for row in summary:
            if row['dir'] == 1 or len(summary) <= 3:
                t = int(row['tdx'])
                vx = px(row['vel'])
                if box1[0] < vx < box1[2]:
                    styled_line(d, [(vx, box1[1]), (vx, box1[3])], COLOURS.get(t, GREY),
                                1, 'dotted')
                    # At the top, beside the line on the side with room, and
                    # below the legend when it would land on it
                    ly = box1[1] + 8 + 20 * (tdxs.index(t) if t in tdxs else 0)
                    if vx > box1[2] - 280:
                        ly += 170
                    label = '%s tuned here' % NAMES.get(t, 'td%d' % t)
                    if vx > (box1[0] + box1[2]) / 2:
                        text(img, (vx - 4, ly), label, small, 'rt')
                    else:
                        text(img, (vx + 4, ly), label, small, 'lt')
        entries = []
        for t in tdxs:
            colour = COLOURS.get(t, GREY)
            for curve, style, label in ((before, 'dotted', 'before'),
                                        (after, 'solid', 'after')):
                if not curve:
                    continue
                # A ripple too fast for the accelerometer has no reading
                # (NaN): its line stops there
                run = []
                for r in curve + [None]:
                    v = None if r is None else r['td%d_amp' % t]
                    if v is None or math.isnan(v):
                        if len(run) > 1:
                            styled_line(d, run, colour, 3, style)
                        run = []
                    else:
                        run.append((px(r['vel']), py(v)))
                entries.append(('%s, %s' % (NAMES.get(t, 'td%d' % t), label), colour, style))
        if not after:
            entries.append(('(no "after" sweep: run VFA_COMPENSATION_GRAPH)', None, ''))
        box_legend(img, d, entries, small, box1[2], box1[1])
    else:
        text(img, ((box1[0] + box1[2]) / 2, (box1[1] + box1[3]) / 2),
             'No speed sweep was recorded', normal, 'mm')

    # Panel 2: each ripple at the speed it was tuned at, before and after
    top2 = box1[3] + 110
    box2 = (x0, top2, x1, top2 + 300)
    text(img, (x0, top2 - 12), 'Each ripple at its worst speed', normal, 'lb')
    groups = sorted({int(r['tdx']) for r in summary})
    if groups:
        stats = []
        for t in groups:
            rows = [r for r in summary if int(r['tdx']) == t]
            stats.append((t, rms([r['before'] for r in rows]), rms([r['after'] for r in rows]),
                          any(r['kept'] for r in rows), rows[0]['vel'], rows[0]['freq']))
        ymax = max(max(s[1], s[2]) for s in stats) or 1.
        yticks, _ = nice_ticks(ymax * 1.2)
        ytop = yticks[-1]

        def py2(v):
            return box2[3] - (box2[3] - box2[1]) * v / ytop

        for v in yticks:
            d.line([(box2[0], py2(v)), (box2[2], py2(v))], fill=LIGHT)
            text(img, (box2[0] - 8, py2(v)), '%g' % v, small, 'rm')
        d.rectangle(box2, outline='black')
        text(img, (box2[0] - 70, (box2[1] + box2[3]) / 2), 'Shaking', small, 'mm', 90)
        slot = (box2[2] - box2[0]) / len(stats)
        bar = min(90, slot / 4)
        for i, (t, b, f, kept, vel, freq) in enumerate(stats):
            cx = box2[0] + slot * (i + 0.5)
            d.rectangle([cx - bar - 4, py2(b), cx - 4, box2[3]], fill=BEFORE_BAR)
            d.rectangle([cx + 4, py2(f), cx + bar + 4, box2[3]],
                        fill=COLOURS.get(t, GREY) if kept else BEFORE_BAR)
            if kept and b > 0:
                label = '%d%% less' % round((1 - f / b) * 100)
            else:
                label = 'left off'
            text(img, (cx, py2(max(b, f)) - 8), label, normal, 'mb')
            text(img, (cx - bar / 2 - 4, box2[3] + 8), 'before', small, 'mt')
            text(img, (cx + bar / 2 + 4, box2[3] + 8), 'after', small, 'mt')
            text(img, (cx, box2[3] + 32), '%s (%.0f mm/s, %.0f Hz)'
                 % (NAMES.get(t, 'td%d' % t), vel, freq), normal, 'mt')
    else:
        text(img, ((box2[0] + box2[2]) / 2, (box2[1] + box2[3]) / 2),
             'No calibration result was recorded', normal, 'mm')

    os.makedirs(a.out_dir, exist_ok=True)
    out = os.path.join(a.out_dir, 'vfa_%s.png' % a.stepper)
    img.save(out)
    print('Graph saved to %s' % out)


if __name__ == '__main__':
    main()
