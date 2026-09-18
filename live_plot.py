#!/usr/bin/env python3
"""Live plots of the logger's CSV, served as a web page.

Run this alongside main.py. It watches data/sensor_log.csv and serves a
page that redraws itself every few seconds, so you can watch the readings
from a browser on another machine -- no desktop needed on the Pi.

    python live_plot.py                      # serve on port 8080
    python live_plot.py --port 9000
    python live_plot.py --csv data/sensor_log.csv
    python live_plot.py --window 30          # show the last 30 minutes

Then open http://<pi-ip>:8080/ on your PC.

Standard library only -- nothing to pip install.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import socket
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"csv": "data/sensor_log.csv", "window": 10}


def load_units(csv_path):
    """Read the units sidecar the logger writes next to the CSV."""
    root, _ = os.path.splitext(csv_path)
    path = f"{root}_units.csv"
    units = {}
    if not os.path.exists(path):
        return units
    try:
        with open(path, newline="") as fh:
            for row in csv.DictReader(fh):
                units[row["column"]] = row["unit"]
    except Exception:
        pass
    return units


def read_rows(csv_path, window_minutes):
    """Return (columns, rows) for the last `window_minutes` of data."""
    if not os.path.exists(csv_path):
        return [], [], {}

    with open(csv_path, newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if not header:
            return [], [], {}
        # Everything between timestamp and status is a numeric channel.
        value_cols = [c for c in header if c not in ("timestamp", "status")]
        rows = list(reader)

    cutoff = None
    if window_minutes:
        cutoff = datetime.now().timestamp() - window_minutes * 60

    out = []
    for row in rows[-20000:]:
        if len(row) != len(header):
            continue
        record = dict(zip(header, row))
        try:
            stamp = datetime.fromisoformat(record["timestamp"]).timestamp()
        except (ValueError, KeyError):
            continue
        if cutoff and stamp < cutoff:
            continue
        point = {"t": stamp * 1000, "status": record.get("status", "")}
        for col in value_cols:
            raw = record.get(col, "")
            try:
                point[col] = float(raw) if raw != "" else None
            except ValueError:
                point[col] = None
        out.append(point)

    return value_cols, out, load_units(csv_path)


PAGE = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Water quality -- live</title>
<style>
  :root { color-scheme: light dark; --bg:#11141a; --fg:#e8eaf0; --dim:#8b94a7;
          --line:#5aa9e6; --grid:#262b36; --card:#171b23; --bad:#ff6b6b; --good:#4ade80; }
  * { box-sizing: border-box; }
  body { margin:0; padding:18px; background:var(--bg); color:var(--fg);
         font:14px/1.5 ui-monospace, "DejaVu Sans Mono", monospace; }
  header { display:flex; align-items:baseline; gap:14px; flex-wrap:wrap;
           margin-bottom:16px; }
  h1 { font-size:17px; margin:0; font-weight:600; letter-spacing:.3px; }
  .pill { padding:2px 9px; border-radius:11px; font-size:12px;
          background:#1f2e22; color:var(--good); }
  .pill.stale { background:#2e1f1f; color:var(--bad); }
  .meta { color:var(--dim); font-size:12px; }
  .grid { display:grid; gap:14px;
          grid-template-columns:repeat(auto-fill, minmax(330px, 1fr)); }
  .card { background:var(--card); border:1px solid var(--grid);
          border-radius:9px; padding:12px 14px; }
  .card h2 { font-size:13px; margin:0 0 2px; font-weight:600; }
  .latest { font-size:25px; font-weight:600; margin:2px 0 8px; }
  .latest span { font-size:13px; color:var(--dim); font-weight:400; }
  canvas { width:100%; height:130px; display:block; }
  .empty { color:var(--dim); padding:40px 0; text-align:center; }
</style></head><body>
<header>
  <h1>Water quality &mdash; live</h1>
  <span class="pill" id="status">waiting</span>
  <span class="meta" id="meta"></span>
</header>
<div class="grid" id="grid"></div>
<div class="empty" id="empty">No data yet. Is main.py running?</div>
<script>
let COLS = [], ROWS = [], UNITS = {};

function draw(canvas, series) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const c = canvas.getContext('2d');
  c.scale(dpr, dpr); c.clearRect(0, 0, w, h);

  const pts = series.filter(p => p.v !== null && p.v !== undefined);
  if (pts.length < 2) {
    c.fillStyle = '#8b94a7'; c.font = '12px monospace';
    c.fillText('not enough points yet', 8, h / 2);
    return;
  }
  const xs = pts.map(p => p.t), ys = pts.map(p => p.v);
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (hi - lo < 1e-9) { lo -= 0.5; hi += 0.5; }
  const pad = (hi - lo) * 0.12; lo -= pad; hi += pad;
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const px = t => 34 + (w - 42) * (t - x0) / Math.max(1, x1 - x0);
  const py = v => 10 + (h - 26) * (1 - (v - lo) / (hi - lo));

  c.strokeStyle = '#262b36'; c.lineWidth = 1;
  c.fillStyle = '#8b94a7'; c.font = '10px monospace';
  for (let i = 0; i <= 2; i++) {
    const v = lo + (hi - lo) * i / 2, y = py(v);
    c.beginPath(); c.moveTo(34, y); c.lineTo(w - 6, y); c.stroke();
    c.fillText(v.toFixed(2), 2, y + 3);
  }

  c.strokeStyle = '#5aa9e6'; c.lineWidth = 1.8;
  c.lineJoin = 'round'; c.beginPath();
  pts.forEach((p, i) => i ? c.lineTo(px(p.t), py(p.v)) : c.moveTo(px(p.t), py(p.v)));
  c.stroke();

  const last = pts[pts.length - 1];
  c.fillStyle = '#ff6b6b'; c.beginPath();
  c.arc(px(last.t), py(last.v), 3.2, 0, 7); c.fill();
}

function render() {
  const grid = document.getElementById('grid');
  document.getElementById('empty').style.display = ROWS.length ? 'none' : 'block';

  COLS.forEach(col => {
    let card = document.getElementById('card-' + col);
    if (!card) {
      card = document.createElement('div');
      card.className = 'card'; card.id = 'card-' + col;
      card.innerHTML = '<h2>' + col + '</h2>' +
        '<div class="latest" id="v-' + col + '">&mdash;</div>' +
        '<canvas id="c-' + col + '"></canvas>';
      grid.appendChild(card);
    }
    const series = ROWS.map(r => ({ t: r.t, v: r[col] }));
    const seen = series.filter(p => p.v !== null && p.v !== undefined);
    const unit = UNITS[col] ? ' <span>' + UNITS[col] + '</span>' : '';
    document.getElementById('v-' + col).innerHTML =
      seen.length ? seen[seen.length - 1].v.toFixed(2) + unit : '&mdash;';
    draw(document.getElementById('c-' + col), series);
  });
}

async function poll() {
  try {
    const r = await fetch('data.json', { cache: 'no-store' });
    const d = await r.json();
    COLS = d.columns; ROWS = d.rows; UNITS = d.units;

    const pill = document.getElementById('status');
    if (!ROWS.length) {
      pill.textContent = 'no data'; pill.className = 'pill stale';
    } else {
      const last = ROWS[ROWS.length - 1];
      const age = (Date.now() - last.t) / 1000;
      pill.textContent = age < 15 ? 'logging' : 'stale ' + Math.round(age) + 's';
      pill.className = age < 15 ? 'pill' : 'pill stale';
      document.getElementById('meta').textContent =
        ROWS.length + ' points \u00b7 ' + new Date(last.t).toLocaleTimeString() +
        (last.status && last.status !== 'ok' ? ' \u00b7 ' + last.status : '');
    }
    render();
  } catch (e) {
    const pill = document.getElementById('status');
    pill.textContent = 'server unreachable'; pill.className = 'pill stale';
  }
}
poll(); setInterval(poll, 3000);
window.addEventListener('resize', render);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/data.json"):
            cols, rows, units = read_rows(STATE["csv"], STATE["window"])
            body = json.dumps(
                {"columns": cols, "rows": rows, "units": units}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass       # don't spam the terminal with one line per poll


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", default="data/sensor_log.csv")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--window", type=float, default=10,
                        help="minutes of history to show (0 = everything)")
    args = parser.parse_args()

    STATE["csv"] = args.csv
    STATE["window"] = args.window

    if not os.path.exists(args.csv):
        print(f"note: {args.csv} does not exist yet -- "
              f"start main.py and the page will fill in.")

    print(f"Serving live plots of {args.csv}")
    print(f"  http://{local_ip()}:{args.port}/")
    print(f"  http://localhost:{args.port}/     (on the Pi itself)")
    print("Ctrl+C to stop.")
    try:
        ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
