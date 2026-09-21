#!/usr/bin/env python3
"""Live plots in the terminal -- runs on the Pi's own console.

No browser, no network, no desktop, nothing to install. Run it on a second
console (Alt+F2) while main.py logs on the first (Alt+F1).

    python3 term_plot.py                  # live, redraws every 2 s
    python3 term_plot.py --window 30      # show the last 30 minutes
    python3 term_plot.py --only do_mg_l ph_volts
    python3 term_plot.py --once           # print one frame and exit

Keys while running:  q = quit   + / - = zoom window in / out
"""

from __future__ import annotations

import argparse
import csv
import os
import time
from datetime import datetime


def load_units(csv_path):
    root, _ = os.path.splitext(csv_path)
    units = {}
    try:
        with open(f"{root}_units.csv", newline="") as fh:
            for row in csv.DictReader(fh):
                units[row["column"]] = row["unit"]
    except Exception:
        pass
    return units


def read_data(csv_path, window_minutes):
    """Return (columns, times, {col: [values]}, last_status)."""
    if not os.path.exists(csv_path):
        return [], [], {}, "waiting for " + csv_path
    with open(csv_path, newline="") as fh:
        # Strip NUL bytes: a power cut mid-write leaves a run of zeros in the
        # file, and csv refuses the whole thing when it meets one.
        reader = csv.reader(line.replace("\0", "") for line in fh)
        header = next(reader, None)
        if not header:
            return [], [], {}, "empty CSV"
        rows = list(reader)[-20000:]

    cols = [c for c in header if c not in ("timestamp", "status")]
    cutoff = time.time() - window_minutes * 60 if window_minutes else None
    times, series, status = [], {c: [] for c in cols}, ""
    for row in rows:
        if len(row) != len(header):
            continue
        rec = dict(zip(header, row))
        try:
            t = datetime.fromisoformat(rec["timestamp"]).timestamp()
        except (ValueError, KeyError):
            continue
        if cutoff and t < cutoff:
            continue
        times.append(t)
        status = rec.get("status", "")
        for c in cols:
            try:
                series[c].append(float(rec[c]) if rec[c] != "" else None)
            except ValueError:
                series[c].append(None)
    return cols, times, series, status


def bucket(values, width):
    """Squeeze a series into `width` columns, averaging each bucket."""
    n = len(values)
    if n <= width:
        return values
    out = []
    for i in range(width):
        chunk = [v for v in values[i * n // width:(i + 1) * n // width]
                 if v is not None]
        out.append(sum(chunk) / len(chunk) if chunk else None)
    return out


def chart_lines(values, width, height):
    """Draw one series as ASCII rows. Returns (lines, lo, hi)."""
    pts = bucket(values, width)
    real = [v for v in pts if v is not None]
    lo, hi = min(real), max(real)
    if hi - lo < 1e-9:
        lo, hi = lo - 0.5, hi + 0.5
    grid = [[" "] * len(pts) for _ in range(height)]

    def row_of(v):
        return height - 1 - round((v - lo) / (hi - lo) * (height - 1))

    prev = None
    for x, v in enumerate(pts):
        if v is None:
            prev = None
            continue
        r = row_of(v)
        if prev is not None and abs(prev - r) > 1:      # join steep jumps
            step = 1 if r > prev else -1
            for rr in range(prev + step, r, step):
                grid[rr][x] = "|"
        grid[r][x] = "*"
        prev = r
    return ["".join(row) for row in grid], lo, hi


def render(csv_path, window, only, cols_total, rows_total):
    """Build the whole screen as a list of strings."""
    cols, times, series, status = read_data(csv_path, window)
    units = load_units(csv_path)
    if only:
        cols = [c for c in cols if c in only]

    live, dead = [], []
    for c in cols:
        (live if any(v is not None for v in series.get(c, [])) else dead).append(c)

    age = time.time() - times[-1] if times else None
    state = ("LOGGING" if age is not None and age < 15
             else f"STALE {int(age)}s" if age is not None else "NO DATA")
    head = (f" Water quality -- {state} -- last {window:g} min, "
            f"{len(times)} pts -- {datetime.now():%H:%M:%S}")
    out = [head[:cols_total], "-" * min(cols_total, 120)]

    if not live:
        out.append(" No readings yet. Is main.py running on the other console?")
        return out

    label_w = 10
    plot_w = max(10, min(cols_total - label_w - 2, 110))
    footer = 2 + (1 if dead else 0)
    per = max(4, (rows_total - len(out) - footer) // len(live))
    chart_h = per - 2

    for c in live:
        vals = series[c]
        last = next(v for v in reversed(vals) if v is not None)
        unit = units.get(c, "")
        out.append(f" {c}   now {last:.3f} {unit}"[:cols_total])
        lines, lo, hi = chart_lines(vals, plot_w, chart_h)
        for i, line in enumerate(lines):
            if i == 0:
                label = f"{hi:>9.2f} "
            elif i == len(lines) - 1:
                label = f"{lo:>9.2f} "
            else:
                label = " " * label_w
            out.append((label + "|" + line)[:cols_total])
        out.append("")

    if dead:
        out.append(" no data: " + ", ".join(dead))
    if status and status != "ok":
        out.append(" ! " + status[:cols_total - 4])
    out.append(" q quit   + / - zoom time window")
    return out


def run_curses(args):
    import curses

    def loop(scr):
        curses.curs_set(0)
        scr.nodelay(True)
        window = args.window
        last_draw = 0.0
        while True:
            key = scr.getch()
            if key in (ord("q"), ord("Q")):
                return
            if key in (ord("+"), ord("=")):
                window = max(1, window / 2); last_draw = 0
            if key in (ord("-"), ord("_")):
                window = min(24 * 60, window * 2); last_draw = 0
            if key == curses.KEY_RESIZE:
                last_draw = 0
            if time.time() - last_draw >= args.refresh:
                h, w = scr.getmaxyx()
                lines = render(args.csv, window, args.only, w - 1, h)
                scr.erase()
                for y, line in enumerate(lines[:h - 1]):
                    try:
                        scr.addstr(y, 0, line)
                    except curses.error:
                        pass
                scr.refresh()
                last_draw = time.time()
            time.sleep(0.05)

    curses.wrapper(loop)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", default="data/sensor_log.csv")
    p.add_argument("--window", type=float, default=10,
                   help="minutes of history on screen (default 10)")
    p.add_argument("--refresh", type=float, default=2.0,
                   help="seconds between redraws (default 2)")
    p.add_argument("--only", nargs="*",
                   help="plot only these columns, e.g. do_mg_l ph_volts")
    p.add_argument("--once", action="store_true",
                   help="print one frame and exit (works over plain SSH)")
    args = p.parse_args()

    if args.once:
        size = os.get_terminal_size() if os.isatty(1) else os.terminal_size((100, 40))
        print("\n".join(render(args.csv, args.window, args.only,
                               size.columns - 1, size.lines)))
        return
    try:
        run_curses(args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
