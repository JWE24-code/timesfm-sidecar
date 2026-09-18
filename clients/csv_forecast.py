#!/usr/bin/env python3
"""Forecast any CSV column with the timesfm-sidecar node.

Stdlib only — no dependencies on the client side.

Examples:
  # forecast column "value" of data.csv, 30 steps ahead, print + write CSV
  python3 csv_forecast.py data.csv value --horizon 30

  # no data handy? generate a synthetic series
  python3 csv_forecast.py --demo value --horizon 48

  # also push actual + forecast to VictoriaMetrics (Grafana-ready)
  python3 csv_forecast.py data.csv value --horizon 30 \
      --metric my_series --vm-url http://victoriametrics:8428
"""
import argparse
import csv
import json
import math
import random
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


def demo_series(n: int) -> list:
    out = []
    for i in range(n):
        v = 50 + 12 * math.sin(i / 6.0) + 5 * math.sin(i / 17.0) + random.uniform(-3, 3) + i * 0.15
        out.append(round(v, 3))
    return out


def read_column(path: str, column: str) -> list:
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if column not in rows[0]:
        raise SystemExit(f"column '{column}' not found; columns: {list(rows[0])}")
    values = []
    for r in rows:
        try:
            values.append(float(r[column]))
        except (TypeError, ValueError):
            continue
    if len(values) < 48:
        raise SystemExit(f"need >= 48 numeric points, got {len(values)}")
    return values


def forecast(api_host: str, api_key: str, values: list, horizon: int) -> dict:
    body = json.dumps({"values": values, "horizon": horizon, "make_positive": False}).encode()
    req = urllib.request.Request(
        f"{api_host.rstrip('/')}/forecast/adhoc", data=body,
        headers={"Content-Type": "application/json", "X-API-Key": api_key},
    )
    return json.load(urllib.request.urlopen(req, timeout=300))


def vm_push(vm_url: str, metric: str, values: list, fc: dict) -> None:
    now = int(time.time())
    step = 900
    lines = [f'{metric}{{kind="actual"}} {v} {(now - len(values) * step + i * step) * 1000}'
             for i, v in enumerate(values)]
    for i, v in enumerate(fc["forecast"]):
        if v is not None:
            lines.append(f'{metric}{{kind="forecast"}} {v} {(now + (i + 1 - len(values)) * step) * 1000}')
    data = ("\n".join(lines) + "\n").encode()
    req = urllib.request.Request(vm_url.rstrip("/") + "/api/v1/import/prometheus", data=data,
                                 headers={"Content-Type": "text/plain"})
    urllib.request.urlopen(req, timeout=30)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csvfile", nargs="?", help="CSV file (omit with --demo)")
    ap.add_argument("column", help="column name to forecast")
    ap.add_argument("--demo", action="store_true", help="use a synthetic series instead of a CSV")
    ap.add_argument("--horizon", type=int, default=30)
    ap.add_argument("--api-host", default="http://localhost:8000")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--metric", default="my_series", help="metric name when pushing to VictoriaMetrics")
    ap.add_argument("--vm-url", default="", help="push actual+forecast to VictoriaMetrics at this URL")
    args = ap.parse_args()

    if args.demo:
        values = demo_series(240)
    elif args.csvfile:
        values = read_column(args.csvfile, args.column)
    else:
        raise SystemExit("give a CSV file or use --demo")

    print(f"forecasting {len(values)} points, horizon {args.horizon} -> {args.api_host}")
    fc = forecast(args.api_host, args.api_key, values, args.horizon)
    last = [v for v in fc["forecast"] if v is not None]
    print(f"inference {fc['elapsed_s']}s | forecast first={last[0]:.2f} last={last[-1]:.2f}")

    out = Path(args.csvfile or "demo").with_name(Path(args.csvfile or "demo").stem + "_forecast.csv")
    start = datetime.now(timezone.utc) - timedelta(minutes=15 * len(values))
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "kind", "value"])
        for i, v in enumerate(values):
            w.writerow([(start + timedelta(minutes=15 * i)).isoformat(), "actual", v])
        for i, v in enumerate(fc["forecast"]):
            if v is not None:
                w.writerow([(start + timedelta(minutes=15 * (len(values) + i))).isoformat(), "forecast", v])
    print(f"wrote {out}")

    if args.vm_url:
        vm_push(args.vm_url, args.metric, values, fc)
        print(f"pushed {args.metric}{{kind=actual|forecast}} to {args.vm_url}")


if __name__ == "__main__":
    main()
