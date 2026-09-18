#!/usr/bin/env python3
import json
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import os

STATE_DIR = Path(os.environ.get("PF_STATE_DIR", Path.home() / ".local/state/portfolio-forecast"))
STATE_FILE = STATE_DIR / "history.json"
API_HOST = os.environ.get("PF_API_HOST", "http://localhost:8000")
VM_URL = os.environ.get("PF_VM_URL", "http://localhost:8428") + "/api/v1/import/prometheus"
SSH_HOST = os.environ.get("PF_DB_SSH_HOST", "db-host")
DB_CONTAINER = os.environ.get("PF_DB_CONTAINER", "analytics-db")
DB_NAME = os.environ.get("PF_DB_NAME", "analytics")
DB_USER = os.environ.get("PF_DB_USER", "analytics")
API_KEY_ITEM = os.environ.get("PF_API_KEY_ITEM", "")
API_KEY = os.environ.get("PF_API_KEY", "")
METRIC = os.environ.get("PF_METRIC", "portfolio_value_eur")
HORIZON_DAYS = int(os.environ.get("PF_HORIZON_DAYS", "30"))
HISTORY_DAYS_SHOWN = int(os.environ.get("PF_HISTORY_DAYS_SHOWN", "120"))
SENTIMENT_THRESHOLD_PCT = float(os.environ.get("PF_SENTIMENT_THRESHOLD_PCT", "3.0"))
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}


def log(msg: str) -> None:
    print(msg, flush=True)


def get_api_key() -> str:
    if API_KEY:
        return API_KEY
    out = subprocess.run(["secret", API_KEY_ITEM, "apiKey"], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"secret {API_KEY_ITEM} failed")
    return out.stdout.strip()


def fetch_holdings() -> list:
    sql = "SELECT symbol, quantity, currency FROM holdings_live WHERE quantity > 0"
    remote = f"docker exec {DB_CONTAINER} psql -U {DB_USER} -d {DB_NAME} -t -A -F'|' -c \"{sql}\""
    out = subprocess.run(["ssh", SSH_HOST, remote],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"psql failed: {out.stderr[:200]}")
    rows = []
    for line in out.stdout.strip().splitlines():
        sym, qty, cur = line.split("|")
        rows.append((sym, float(qty), cur))
    return rows


def yahoo_closes(ticker: str) -> dict:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.request.quote(ticker)}?range=2y&interval=1d"
    req = urllib.request.Request(url, headers=UA)
    d = json.load(urllib.request.urlopen(req, timeout=25))
    res = d["chart"]["result"][0]
    stamps = res["timestamp"]
    closes = res["indicators"]["quote"][0]["close"]
    out = {}
    for ts, c in zip(stamps, closes):
        if c is not None:
            out[datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()] = float(c)
    return out


def yahoo_candidates(sym: str, cur: str) -> list:
    cands = []
    if "/" in sym:
        cands.append(sym.replace("/", "-"))
    if cur == "USD":
        cands.append(sym)
    elif cur == "GBP":
        cands.append(sym if "." in sym else sym + ".L")
    else:
        cands.append(sym)
        cands.extend(sym + s for s in (".AS", ".BR", ".DE", ".PA", ".MC", ".MI", ".SW"))
    seen = set()
    return [c for c in cands if not (c in seen or seen.add(c))]


def fetch_symbol_closes(sym: str, cur: str) -> dict:
    last_err = None
    for cand in yahoo_candidates(sym, cur):
        try:
            return yahoo_closes(cand)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"yahoo fetch failed for position '{sym[:2]}**' (all candidates): {last_err}")


def build_series(holdings: list) -> tuple:
    per_symbol = {}
    fx = {}
    for sym, qty, cur in holdings:
        closes = fetch_symbol_closes(sym, cur)
        if cur == "USD":
            rate = fx.setdefault("EURUSD=X", yahoo_closes("EURUSD=X"))
            closes = {d: p / rate[d] for d, p in closes.items() if d in rate}
        elif cur == "GBP":
            rate = fx.setdefault("GBPEUR=X", yahoo_closes("GBPEUR=X"))
            closes = {d: p * rate[d] for d, p in closes.items() if d in rate}
        per_symbol[sym] = {"qty": qty, "closes": closes}
    all_dates = sorted({d for s in per_symbol.values() for d in s["closes"]})
    start = max(min(s["closes"]) for s in per_symbol.values())
    dates = [d for d in all_dates if d >= start]
    values = []
    for d in dates:
        total = 0.0
        for s in per_symbol.values():
            p = s["closes"].get(d)
            if p is None:
                p = s.get("last")
                if p is None:
                    raise RuntimeError(f"gap before first fill at {d}")
            s["last"] = p
            total += s["qty"] * p
        values.append((d, round(total, 2)))
    return values, per_symbol


def api_forecast(values: list) -> dict:
    key = get_api_key()
    body = json.dumps({"values": values, "horizon": HORIZON_DAYS, "make_positive": True}).encode()
    deadline = time.time() + 600
    last_err = None
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                f"{API_HOST}/forecast/adhoc", data=body,
                headers={"Content-Type": "application/json", "X-API-Key": key},
            )
            return json.load(urllib.request.urlopen(req, timeout=120))
        except urllib.error.HTTPError as e:
            if e.code == 503:
                log("  api busy (model loading/gaming), retry in 30s")
                time.sleep(30)
                continue
            raise RuntimeError(f"api {e.code}: {e.read()[:200]}")
        except Exception as e:
            last_err = e
            time.sleep(30)
    raise RuntimeError(f"api unreachable: {last_err}")


def vm_push(lines: list) -> None:
    if not lines:
        return
    req = urllib.request.Request(VM_URL, data=("\n".join(lines) + "\n").encode(),
                                 headers={"Content-Type": "text/plain"})
    urllib.request.urlopen(req, timeout=30)


def ts_ms(iso_date: str) -> int:
    return int(datetime.strptime(iso_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def next_business_days(last_date: str, n: int) -> list:
    d = datetime.strptime(last_date, "%Y-%m-%d").date()
    out = []
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.isoformat())
    return out


def push_all(series: list, fc: dict) -> None:
    recent = series[-HISTORY_DAYS_SHOWN:]
    lines = [f'{METRIC}{{kind="actual"}} {v} {ts_ms(d)}' for d, v in recent]
    fdates = next_business_days(series[-1][0], HORIZON_DAYS)
    last_date = datetime.strptime(series[-1][0], "%Y-%m-%d").date()
    shifted = []
    d = last_date
    while len(shifted) < HORIZON_DAYS:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            shifted.append(d.isoformat())
    shifted.reverse()
    q = fc.get("quantiles")
    for i, v in enumerate(fc["forecast"]):
        if v is None:
            continue
        t = ts_ms(shifted[i])
        lines.append(f'{METRIC}{{kind="forecast"}} {v} {t}')
        if isinstance(q, list) and i < len(q) and isinstance(q[i], list) and q[i]:
            if q[i][0] is not None:
                lines.append(f'{METRIC}{{kind="band_low"}} {q[i][0]} {t}')
            if q[i][-1] is not None:
                lines.append(f'{METRIC}{{kind="band_high"}} {q[i][-1]} {t}')
    vm_push(lines)
    last_fc = [v for v in fc["forecast"] if v is not None]
    if last_fc:
        vm_push([f'{METRIC}_forecast_last {last_fc[-1]} {int(time.time() * 1000)}'])
    log(f"  pushed: {len(recent)} actual + {len(fc['forecast'])} forecast samples")


def push_sentiment(per_symbol: dict) -> None:
    now_ms = int(time.time() * 1000)
    lines = []
    counts = {"bullish": 0, "bearish": 0, "neutral": 0}
    for sym, info in per_symbol.items():
        closes = info["closes"]
        series = [v for _, v in sorted(closes.items())][-500:]
        if len(series) < 48:
            log(f"  sentiment: skipping one position (only {len(series)} pts)")
            continue
        fc = api_forecast(series)
        last_fc = [v for v in fc["forecast"] if v is not None]
        if not last_fc:
            continue
        ret = (last_fc[-1] - series[-1]) / series[-1] * 100
        signal = 1 if ret > SENTIMENT_THRESHOLD_PCT else (-1 if ret < -SENTIMENT_THRESHOLD_PCT else 0)
        counts["bullish" if signal == 1 else "bearish" if signal == -1 else "neutral"] += 1
        sm = os.environ.get('PF_STOCK_METRIC', 'stock_forecast')
        lines.append(f'{sm}_return_pct{{symbol="{sym}"}} {ret:.3f} {now_ms}')
        lines.append(f'{sm}_signal{{symbol="{sym}"}} {signal} {now_ms}')
        for back in range(15, 24 * 60 + 15, 15):
            t = now_ms - back * 60000
            lines.append(f'{sm}_return_pct{{symbol="{sym}"}} {ret:.3f} {t}')
            lines.append(f'{sm}_signal{{symbol="{sym}"}} {signal} {t}')
    vm_push(lines)
    log(f"  sentiment: {counts['bullish']} bullish / {counts['neutral']} neutral / {counts['bearish']} bearish ({len(counts) and sum(counts.values())} positions)")


def push_accuracy(series: list) -> None:
    if not STATE_FILE.exists():
        log("  accuracy: no history yet, skipped")
        return
    hist = json.loads(STATE_FILE.read_text())
    actual = dict(series)
    now = datetime.now(timezone.utc)
    target = now - timedelta(days=7)
    best = None
    for run in hist:
        made = datetime.fromisoformat(run["made_at"])
        if best is None or abs((made - target).total_seconds()) < abs((datetime.fromisoformat(best["made_at"]) - target).total_seconds()):
            best = run
    made = datetime.fromisoformat(best["made_at"])
    age_days = (now - made).total_seconds() / 86400
    if age_days < 3:
        log("  accuracy: oldest run too recent, skipped")
        return
    lines = []
    errs = []
    for d, v in zip(best["dates"], best["values"]):
        if v is None or d not in actual:
            continue
        errs.append(abs(actual[d] - float(v)))
        lines.append(f'{METRIC}{{kind="forecast_7d_ago"}} {v} {ts_ms(d)}')
    if len(errs) >= 3:
        mae = sum(errs) / len(errs)
        lines.append(f'{METRIC}_mae7 {mae:.2f} {int(now.timestamp() * 1000)}')
        vm_push(lines)
        log(f"  accuracy: {len(errs)} matched buckets (run {best['made_at'][:10]}), MAE pushed")
    else:
        log("  accuracy: not enough realized buckets yet")


def main() -> None:
    log("fetching holdings + prices...")
    holdings = fetch_holdings()
    log(f"  {len(holdings)} positions")
    series, per_symbol = build_series(holdings)
    log(f"  reconstructed series: {len(series)} trading days, {series[0][0]} .. {series[-1][0]}")
    if len(series) < 60:
        raise RuntimeError("not enough history")
    values = [v for _, v in series]
    log("forecasting (ephemeral on MyDesktop)...")
    fc = api_forecast(values)
    log(f"  horizon {fc['horizon']} in {fc['elapsed_s']}s")
    push_all(series, fc)
    log("per-position sentiment...")
    push_sentiment(per_symbol)
    push_accuracy(series)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    hist = []
    if STATE_FILE.exists():
        hist = json.loads(STATE_FILE.read_text())
    hist.append({
        "made_at": datetime.now(timezone.utc).isoformat(),
        "dates": [d for d, _ in series[-HORIZON_DAYS * 2:]],
        "values": values[-HORIZON_DAYS * 2:],
    })
    hist = hist[-15:]
    STATE_FILE.write_text(json.dumps(hist))
    log("done")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FAILED: {e}")
        sys.exit(1)
