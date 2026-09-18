import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from fastapi import Header, HTTPException

HOME = Path(os.environ.get("TIMESFM_HOME", r"C:\timesfm"))
KEYS_FILE = HOME / "app" / "api_keys.json"
AUDIT_FILE = HOME / "data" / "audit.log"
CONFIG_FILE = HOME / "app" / "config.json"

_rate: dict = {}


def _job_token() -> str:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("job_token", "")
    except Exception:
        return ""


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def load_keys() -> dict:
    if not KEYS_FILE.exists():
        return {}
    return json.loads(KEYS_FILE.read_text())


def _audit(name: str, scope: str, status: int) -> None:
    line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "key": name, "scope": scope, "status": status})
    try:
        with AUDIT_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _rate_ok(name: str, limit: int = 60) -> bool:
    now = time.time()
    hits = [t for t in _rate.get(name, []) if now - t < 60]
    if len(hits) >= limit:
        _rate[name] = hits
        return False
    hits.append(now)
    _rate[name] = hits
    return True


def require(scope: str):
    def dep(x_api_key: str = Header(default="")):
        if _job_token() and hmac.compare_digest(x_api_key, _job_token()):
            _audit("local-job", scope, 200)
            return {"name": "local-job", "scopes": ["admin"]}
        for name, info in load_keys().items():
            if hmac.compare_digest(key_hash(x_api_key), info.get("hash", "")):
                if scope not in info.get("scopes", []) and "admin" not in info.get("scopes", []):
                    _audit(name, scope, 403)
                    raise HTTPException(status_code=403, detail="scope not allowed")
                if not _rate_ok(name):
                    _audit(name, scope, 429)
                    raise HTTPException(status_code=429, detail="rate limited")
                _audit(name, scope, 200)
                return {"name": name, "scopes": info.get("scopes", [])}
        _audit("unknown", scope, 401)
        raise HTTPException(status_code=401, detail="invalid api key")

    return dep
