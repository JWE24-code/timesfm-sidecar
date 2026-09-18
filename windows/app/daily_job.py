import logging
import sys
import time

import httpx

HOME = os.environ.get("TIMESFM_HOME", r"C:\timesfm")
sys.path.insert(0, HOME + r"\app")
CONFIG_PATH = HOME + r"\app\config.json"
import json
import os

CONFIG = json.loads(open(CONFIG_PATH, encoding="utf-8").read())

logging.basicConfig(
    filename=HOME + r"\data\job.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("job")

BASE = os.environ.get("TIMESFM_API", "http://127.0.0.1:8000")
HEADERS = {"Content-Type": "application/json", "X-API-Key": CONFIG["job_token"]}


def call(path: str, body: dict, timeout: int = 900) -> dict:
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            r = httpx.post(BASE + path, json=body, headers=HEADERS, timeout=120)
            if r.status_code == 503:
                detail = ""
                try:
                    detail = r.json().get("detail", "")
                except Exception:
                    pass
                log.info("503 from %s (%s), retrying in 30s", path, detail)
                time.sleep(30)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            log.warning("call %s failed: %s, retrying in 30s", path, e)
            time.sleep(30)
    raise RuntimeError(f"{path} did not succeed within timeout: {last_err}")


def run() -> None:
    res = call("/ingest", {"days": 7})
    log.info("ingest: %s", res)
    for pair in CONFIG["entities"]:
        for key, metric in (("power", "watt"), ("energy", "kwh")):
            entity = pair.get(key)
            if not entity:
                continue
            res = call("/forecast", {"entity": entity, "metric": metric, "horizon": 96, "context_days": 7})
            log.info(
                "forecast %s [%s]: %s pts, %.2fs",
                entity,
                metric,
                res.get("points_used"),
                res.get("elapsed_s", 0),
            )


if __name__ == "__main__":
    try:
        run()
        log.info("job ok")
    except Exception as e:
        log.error("job failed: %s", e)
        sys.exit(1)
