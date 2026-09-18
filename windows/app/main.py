import json
import os
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import httpx
import numpy as np
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import auth
from auth import require

log = logging.getLogger("tmfm")

HOME = Path(os.environ.get("TIMESFM_HOME", r"C:\timesfm"))
APP_DIR = HOME / "app"
DATA_DIR = HOME / "data"
DB_FILE = DATA_DIR / "energy.duckdb"
WATCHER_STATE = Path(r"C:\timesfm\watcher_state.json")
CONFIG = json.loads((APP_DIR / "config.json").read_text(encoding="utf-8"))

app = FastAPI(title="TimesFM forecast API", docs_url=None, redoc_url=None)

VIZ_FILE = APP_DIR / "viz.html"


@app.get("/")
def root():
    return FileResponse(VIZ_FILE, media_type="text/html")

_model = None
_model_err = None


def _load_model():
    global _model, _model_err
    try:
        from timesfm import TimesFM3Forecaster

        _model = TimesFM3Forecaster.from_pretrained("google/timesfm-3.0-pytorch", device="cpu")
    except Exception as e:
        _model_err = str(e)


threading.Thread(target=_load_model, daemon=True).start()


def _finite(value):
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


def get_model():
    if _model is None:
        raise HTTPException(status_code=503, detail=f"model not loaded: {_model_err or 'loading'}")
    return _model


def db():
    return duckdb.connect(str(DB_FILE))


def watcher_gaming() -> bool:
    try:
        return bool(json.loads(WATCHER_STATE.read_text()).get("gaming"))
    except Exception:
        return False


def _fetch_history(entity: str, start: datetime, headers: dict, days: int) -> list:
    all_pts = []
    seen = set()
    base = CONFIG["ha_url"].rstrip("/")
    for i in range(days):
        day_start = start + timedelta(days=i)
        url = f"{base}/api/history/period/{day_start.isoformat()}"
        r = httpx.get(url, params={"filter_entity_id": entity, "significant_changes_only": "false"}, headers=headers, timeout=60)
        r.raise_for_status()
        data = r.json()
        for p in (data[0] if data else []):
            key = p.get("last_changed") or p.get("last_updated")
            if key not in seen:
                seen.add(key)
                all_pts.append(p)
    return all_pts


BUCKET_MINUTES = 15


def _bucketize(points: list, agg: str) -> dict:
    buckets = {}
    for p in points:
        try:
            val = float(p["state"])
        except (TypeError, ValueError):
            continue
        lc = p.get("last_changed") or p.get("last_updated")
        ts = datetime.fromisoformat(lc).astimezone(timezone.utc).replace(tzinfo=None)
        m = (ts.minute // BUCKET_MINUTES) * BUCKET_MINUTES
        key = ts.replace(minute=m, second=0, microsecond=0)
        b = buckets.setdefault(key, {"sum": 0.0, "n": 0, "last": val})
        b["sum"] += val
        b["n"] += 1
        b["last"] = val
    return buckets


def _fill_range(buckets: dict, agg: str) -> dict:
    out = {}
    if not buckets:
        return out
    keys = sorted(buckets)
    step = timedelta(minutes=BUCKET_MINUTES)
    cur = keys[0]
    last_val = None
    while cur <= keys[-1]:
        b = buckets.get(cur)
        if b is None:
            out[cur] = round(last_val, 4) if (last_val is not None and agg == "delta") else (round(last_val, 2) if last_val is not None else None)
        elif agg == "mean":
            out[cur] = round(b["sum"] / b["n"], 2)
            last_val = out[cur]
        else:
            if last_val is not None:
                out[cur] = round(b["last"] - last_val, 4) if b["last"] >= last_val else round(b["last"], 4)
            last_val = b["last"]
        cur += step
    return {k: v for k, v in out.items() if v is not None}


def _init_db(con):
    con.execute(
        "CREATE TABLE IF NOT EXISTS hourly "
        "(entity VARCHAR, metric VARCHAR, ts TIMESTAMP, value DOUBLE, PRIMARY KEY (entity, metric, ts))"
    )
    cols = [
        r[0]
        for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'forecasts'"
        ).fetchall()
    ]
    if cols and "metric" not in cols:
        con.execute("DROP TABLE forecasts")
        cols = []
    if not cols:
        con.execute("CREATE SEQUENCE IF NOT EXISTS forecasts_seq")
        con.execute(
            "CREATE TABLE forecasts "
            "(id BIGINT DEFAULT nextval('forecasts_seq'), entity VARCHAR, metric VARCHAR, "
            "created TIMESTAMP, horizon INTEGER, elapsed_s DOUBLE, result_json VARCHAR)"
        )
    con.execute("DROP TABLE IF EXISTS energy_hourly")


class IngestReq(BaseModel):
    days: int = Field(default=7, ge=1, le=9)


class ForecastReq(BaseModel):
    entity: str
    metric: str = Field(default="watt", pattern="^(kwh|watt)$")
    horizon: int = Field(default=24, ge=1, le=168)
    context_days: int = Field(default=7, ge=2, le=30)


METRIC_BASE = {"watt": "home_power_watts", "kwh": "home_energy_kwh"}


def _vm_push(lines: list) -> None:
    if not lines or not CONFIG.get("vm_url"):
        return
    try:
        r = httpx.post(
            CONFIG["vm_url"].rstrip("/") + "/api/v1/import/prometheus",
            content=("\n".join(lines) + "\n").encode(),
            headers={"Content-Type": "text/plain"},
            timeout=30,
        )
        r.raise_for_status()
        log.info("pushed %d samples to victoria", len(lines))
    except Exception as e:
        log.warning("victoria push failed: %s", e)


def _ts_ms(ts: datetime) -> int:
    return int(ts.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _push_actuals(days_recent: int = 2) -> None:
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_recent)
    con = db()
    rows = con.execute(
        "SELECT entity, metric, ts, value FROM hourly WHERE ts >= ? ORDER BY entity, metric, ts",
        [since],
    ).fetchall()
    con.close()
    lines = []
    for entity, metric, ts, value in rows:
        if value is None:
            continue
        lines.append(f'{METRIC_BASE[metric]}{{entity="{entity}",kind="actual"}} {value} {_ts_ms(ts)}')
    _vm_push(lines)


def _push_forecast(entity: str, metric: str, last_ts: datetime, horizon: int, forecast: list, quantiles) -> None:
    base = METRIC_BASE[metric]
    horizon_td = timedelta(minutes=BUCKET_MINUTES * horizon)
    lines = []
    for i, v in enumerate(forecast):
        if v is None:
            continue
        ts = _ts_ms(last_ts + timedelta(minutes=BUCKET_MINUTES * (i + 1)) - horizon_td)
        lines.append(f'{base}{{entity="{entity}",kind="forecast"}} {v} {ts}')
        if isinstance(quantiles, list) and len(quantiles) > i and isinstance(quantiles[i], list) and quantiles[i]:
            lo, hi = quantiles[i][0], quantiles[i][-1]
            if lo is not None:
                lines.append(f'{base}{{entity="{entity}",kind="band_low"}} {lo} {ts}')
            if hi is not None:
                lines.append(f'{base}{{entity="{entity}",kind="band_high"}} {hi} {ts}')
    _vm_push(lines)


def _do_ingest(days: int) -> dict:
    start = datetime.now(timezone.utc) - timedelta(days=days)
    headers = {"Authorization": f"Bearer {CONFIG['ha_token']}"}
    con = db()
    _init_db(con)
    ingested = {}
    try:
        con.execute("BEGIN")
        for pair in CONFIG["entities"]:
            for metric, entity, agg in (("kwh", pair["energy"], "delta"), ("watt", pair["power"], "mean")):
                if not entity:
                    continue
                pts = _fetch_history(entity, start, headers, days)
                buckets = _bucketize(pts, agg)
                filled = _fill_range(buckets, agg)
                rows = [(entity, metric, ts, v) for ts, v in filled.items()]
                if rows:
                    con.executemany("INSERT OR REPLACE INTO hourly VALUES (?, ?, ?, ?)", rows)
                ingested[f"{metric}:{entity}"] = len(rows)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        con.close()
        raise
    con.close()
    _push_actuals()
    return {"ingested": ingested}


def _push_accuracy(entity: str, metric: str) -> None:
    try:
        con = db()
        rows = con.execute(
            "SELECT created, result_json FROM forecasts WHERE entity = ? AND metric = ? ORDER BY id DESC LIMIT 40",
            [entity, metric],
        ).fetchall()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        target = now - timedelta(hours=24)
        old = None
        for created, rj in rows:
            if created <= target:
                old = (created, json.loads(rj))
                break
        if old is None and rows and (now - rows[-1][0]) > timedelta(minutes=30):
            old = (rows[-1][0], json.loads(rows[-1][1]))
        if old is None:
            con.close()
            return
        created, data = old
        vals = data.get("forecast") or []
        fb = data.get("first_bucket_ts")
        if fb:
            first = datetime.fromisoformat(fb)
        else:
            first = created.replace(second=0, microsecond=0)
            first += timedelta(minutes=(15 - (first.minute % 15)) % 15 or 15)
        base = METRIC_BASE[metric]
        lines = []
        matched = []
        for i, v in enumerate(vals):
            if v is None:
                continue
            ts = first + timedelta(minutes=BUCKET_MINUTES * i)
            row = con.execute(
                "SELECT value FROM hourly WHERE entity = ? AND metric = ? AND ts = ?",
                [entity, metric, ts],
            ).fetchone()
            if row and row[0] is not None:
                matched.append(abs(float(row[0]) - float(v)))
                lines.append(f'{base}{{entity="{entity}",kind="forecast_24h_ago"}} {v} {_ts_ms(ts)}')
        con.close()
        if len(matched) >= 12:
            mae = sum(matched) / len(matched)
            lines.append(f'{base}_mae24{{entity="{entity}"}} {mae:.2f} {_ts_ms(now)}')
            _vm_push(lines)
            log.info("accuracy %s [%s]: mae24=%.2f over %d buckets", entity, metric, mae, len(matched))
    except Exception as e:
        log.warning("accuracy push failed: %s", e)


def _do_forecast(entity: str, metric: str, horizon: int, context_days: int) -> dict:
    if watcher_gaming():
        raise HTTPException(status_code=503, detail="paused: gaming detected")
    model = get_model()
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=context_days)
    con = db()
    _init_db(con)
    rows = con.execute(
        "SELECT ts, value FROM hourly WHERE entity = ? AND metric = ? AND ts >= ? ORDER BY ts",
        [entity, metric, since],
    ).fetchall()
    con.close()
    values = [float(r[1]) for r in rows]
    if len(values) < 48:
        raise HTTPException(status_code=400, detail=f"not enough history: {len(values)} points, need >= 48")
    last_ts = rows[-1][0]
    t0 = time.time()
    result = model.predict(context=np.asarray(values, dtype=np.float32), horizon=horizon, return_quantiles=True)
    elapsed = round(time.time() - t0, 2)
    fc = np.atleast_2d(np.asarray(result.forecast, dtype=float))
    forecast_values = _finite(fc[0].tolist() if fc.shape[0] == 1 else fc.tolist())
    quantiles = None
    try:
        if isinstance(result.quantiles, dict):
            quantiles = {k: np.asarray(v).tolist() for k, v in result.quantiles.items()}
        else:
            quantiles = np.asarray(result.quantiles, dtype=float).tolist()
    except Exception:
        pass
    quantiles = _finite(quantiles)
    first_bucket = last_ts + timedelta(minutes=BUCKET_MINUTES)
    con = db()
    con.execute(
        "INSERT INTO forecasts (entity, metric, created, horizon, elapsed_s, result_json) VALUES (?, ?, ?, ?, ?, ?)",
        [
            entity,
            metric,
            datetime.now(timezone.utc).replace(tzinfo=None),
            horizon,
            elapsed,
            json.dumps({
                "forecast": forecast_values,
                "quantiles": quantiles,
                "points_used": len(values),
                "first_bucket_ts": first_bucket.isoformat(),
            }),
        ],
    )
    con.close()
    _push_accuracy(entity, metric)
    _push_forecast(entity, metric, last_ts, horizon, forecast_values, quantiles)
    return {
        "entity": entity,
        "metric": metric,
        "horizon": horizon,
        "points_used": len(values),
        "elapsed_s": elapsed,
        "forecast": forecast_values,
        "quantiles": quantiles,
    }


@app.get("/health")
def health():
    gaming = watcher_gaming()
    db_ok = False
    n = 0
    try:
        con = db()
        _init_db(con)
        n = con.execute("SELECT COUNT(DISTINCT entity) FROM hourly").fetchone()[0]
        con.close()
        db_ok = True
    except Exception:
        pass
    return {
        "status": "ok" if (_model is not None and db_ok) else "starting",
        "model_loaded": _model is not None,
        "model_error": _model_err,
        "gaming": gaming,
        "db_ok": db_ok,
        "entities": n,
    }


@app.post("/ingest")
def ingest(req: IngestReq, key=Depends(require("ingest"))):
    return _do_ingest(req.days)


class AdhocReq(BaseModel):
    values: list[float] = Field(min_length=48, max_length=4000)
    horizon: int = Field(default=30, ge=1, le=365)
    make_positive: bool = True


@app.post("/forecast/adhoc")
def forecast_adhoc(req: AdhocReq, key=Depends(require("forecast"))):
    if watcher_gaming():
        raise HTTPException(status_code=503, detail="paused: gaming detected")
    model = get_model()
    t0 = time.time()
    result = model.predict(
        context=np.asarray(req.values, dtype=np.float32),
        horizon=req.horizon,
        return_quantiles=True,
        make_positive=req.make_positive,
    )
    elapsed = round(time.time() - t0, 2)
    fc = np.atleast_2d(np.asarray(result.forecast, dtype=float))
    forecast_values = _finite(fc[0].tolist() if fc.shape[0] == 1 else fc.tolist())
    quantiles = None
    try:
        quantiles = np.asarray(result.quantiles, dtype=float).tolist()
    except Exception:
        pass
    quantiles = _finite(quantiles)
    return {
        "horizon": req.horizon,
        "points_used": len(req.values),
        "elapsed_s": elapsed,
        "forecast": forecast_values,
        "quantiles": quantiles,
    }


@app.get("/entities")
def entities(key=Depends(require("read"))):
    con = db()
    _init_db(con)
    rows = con.execute(
        "SELECT entity, metric, COUNT(*), MIN(ts), MAX(ts) FROM hourly GROUP BY entity, metric ORDER BY entity, metric"
    ).fetchall()
    con.close()
    return {
        "series": [
            {"entity": r[0], "metric": r[1], "points": r[2], "from": r[3].isoformat(), "to": r[4].isoformat()}
            for r in rows
        ]
    }


@app.get("/series")
def series(entity: str, metric: str = "watt", days: int = 7, key=Depends(require("read"))):
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    con = db()
    _init_db(con)
    rows = con.execute(
        "SELECT ts, value FROM hourly WHERE entity = ? AND metric = ? AND ts >= ? ORDER BY ts",
        [entity, metric, since],
    ).fetchall()
    con.close()
    return {"entity": entity, "metric": metric, "points": [{"ts": r[0].isoformat(), "value": r[1]} for r in rows]}


@app.post("/forecast")
def forecast(req: ForecastReq, key=Depends(require("forecast"))):
    return _do_forecast(req.entity, req.metric, req.horizon, req.context_days)
