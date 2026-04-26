import os
import time
import threading
import requests
from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
from collections import defaultdict

app = Flask(__name__, static_folder="static")
CORS(app)

TAOSTATS_API_KEY = os.environ.get("TAOSTATS_API_KEY", "")
TAOSTATS_BASE = "https://api.taostats.io"
COLDKEY = os.environ.get("COLDKEY", "5Cexeg7deNSTzsqMKuBmvc9JHGHymuL4SdjAA9Jw4eeHUphb")
HEADERS = {"Authorization": TAOSTATS_API_KEY}

# Rate limit: 5 req/min → 1 request every 13s to be safe
REQUEST_GAP = 13          # seconds between TaoStats API calls
CACHE_TTL = 15 * 60       # refresh cache every 15 minutes
MAX_RETRIES = 3           # retries on 429 or transient errors
RETRY_BACKOFF = 30        # seconds for first retry; doubled each subsequent attempt

# ── In-memory cache ───────────────────────────────────────────────────────────
_cache = {
    "wallet": None,
    "pool": {},
    "yield": {},
    "flow": {},
    "pool_updated": {},     # netuid -> unix ts of last successful pool cache
    "yield_updated": {},    # netuid -> unix ts of last successful yield cache
    "flow_updated": {},     # netuid -> unix ts of last successful flow cache
    "last_updated": None,
    "status": "pending",    # "pending" | "refreshing" | "ready" | "error"
    "error": None,
    "fetch_errors": {},     # "kind:netuid" -> last error message; cleared on success
}
_cache_lock = threading.Lock()


# ── Rate Limiter ──────────────────────────────────────────────────────────────

class RateLimiter:
    """Global rate limiter ensuring no more than 5 requests per minute."""
    def __init__(self, requests_per_minute):
        self.min_gap = 60.0 / requests_per_minute  # 12 seconds for 5 req/min
        self.last_request_time = 0
        self.lock = threading.Lock()
    
    def wait_and_acquire(self):
        """Block until enough time has passed since last request, then proceed."""
        with self.lock:
            now = time.time()
            elapsed = now - self.last_request_time
            if elapsed < self.min_gap:
                wait_time = self.min_gap - elapsed
                time.sleep(wait_time)
            self.last_request_time = time.time()

rate_limiter = RateLimiter(5)  # 5 requests per minute


# ── Helpers ───────────────────────────────────────────────────────────────────

def first_val(d, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


def safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def taostats_get(url, max_retries=MAX_RETRIES):
    """
    GET against TaoStats with retry-on-429 and exponential backoff.
    Uses global rate_limiter to enforce 5 req/min across all threads.
    Retries 429 and connection errors up to max_retries times.
    Raises on final failure so callers can record per-subnet errors.
    """
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            # Wait for rate limit before making request
            rate_limiter.wait_and_acquire()
            
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 429:
                if attempt < max_retries:
                    wait = RETRY_BACKOFF * (2 ** attempt)
                    app.logger.warning(
                        "Rate limited on %s — backing off %ds (attempt %d/%d)",
                        url, wait, attempt + 1, max_retries
                    )
                    time.sleep(wait)
                    continue
                raise requests.HTTPError(f"429 Rate Limited after {max_retries} retries")
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last_err = e
            # Retry only on connection-level errors and 5xx, not 4xx (other than 429 above)
            status = getattr(getattr(e, "response", None), "status_code", None)
            retryable = status is None or status >= 500
            if retryable and attempt < max_retries:
                wait = RETRY_BACKOFF * (2 ** attempt)
                app.logger.warning(
                    "Request error on %s: %s — retrying in %ds (attempt %d/%d)",
                    url, e, wait, attempt + 1, max_retries
                )
                time.sleep(wait)
                continue
            raise
    raise last_err  # pragma: no cover


# ── Data parsers ──────────────────────────────────────────────────────────────

def parse_wallet(raw):
    grouped = defaultdict(lambda: {
        "netuid": None, "alpha_balance": 0.0, "tao_value": 0.0, "validators": [],
    })
    for item in raw.get("data", []):
        netuid = item.get("netuid")
        if netuid is None:
            continue
        balance_tao = int(item.get("balance", 0)) / 1_000_000_000
        balance_as_tao = int(item.get("balance_as_tao", 0)) / 1_000_000_000
        g = grouped[netuid]
        g["netuid"] = netuid
        g["alpha_balance"] = round(g["alpha_balance"] + balance_tao, 6)
        g["tao_value"] = round(g["tao_value"] + balance_as_tao, 6)
        hotkey_obj = item.get("hotkey", {})
        hotkey_ss58 = hotkey_obj.get("ss58", "") if isinstance(hotkey_obj, dict) else str(hotkey_obj)
        g["validators"].append({
            "hotkey": hotkey_ss58,
            "hotkey_name": item.get("hotkey_name", ""),
            "alpha_balance": round(balance_tao, 6),
            "tao_value": round(balance_as_tao, 6),
            "validator_rank": item.get("validator_rank") or item.get("subnet_rank"),
        })
    return sorted(grouped.values(), key=lambda x: x["tao_value"], reverse=True)


def parse_pool(raw, netuid):
    d = raw.get("data", raw)
    if isinstance(d, list):
        matches = [x for x in d if isinstance(x, dict) and str(x.get("netuid")) == str(netuid)]
        d = matches[0] if matches else (d[0] if d else {})
    elif not isinstance(d, dict):
        d = {}
    subnet_obj = d.get("subnet") or {}
    name = (
        first_val(d, "name", "subnet_name", "title", "token_name", "symbol")
        or first_val(subnet_obj, "name", "subnet_name", "title")
    )
    price = safe_float(first_val(d, "price", "last_price", "alpha_price", "token_price",
                                 "current_price", "alpha_token_price"))
    change_24h = safe_float(first_val(d, "price_change_1_day", "price_change_24h", "change_24h",
                                      "alpha_price_change_1_day", "token_price_change_1_day",
                                      "price_change_day"))
    change_7d = safe_float(first_val(d, "price_change_1_week", "price_change_7d", "change_7d",
                                     "token_price_change_1_week", "alpha_price_change_1_week",
                                     "alpha_price_change_7d", "price_change_week"))
    change_30d = safe_float(first_val(d, "price_change_1_month", "price_change_30d", "change_30d",
                                      "price_change_4_weeks", "token_price_change_1_month",
                                      "alpha_price_change_1_month", "alpha_price_change_30d",
                                      "price_change_month"))
    root_prop = safe_float(first_val(d, "root_prop", "emission", "emission_share",
                                     "tao_emission", "emission_ratio", "tao_emission_ratio"))
    emission_pct = None
    if root_prop is not None:
        emission_pct = root_prop if root_prop > 1.0 else root_prop * 100
    return {"netuid": netuid, "name": name, "price": price,
            "change_24h": change_24h, "change_7d": change_7d, "change_30d": change_30d,
            "emission": root_prop, "emission_pct": emission_pct}


def parse_yield(pages_data, netuid):
    validators = []
    for v in pages_data:
        hotkey_obj = v.get("hotkey", {})
        hotkey = hotkey_obj.get("ss58", "") if isinstance(hotkey_obj, dict) else str(hotkey_obj)
        apy_raw = first_val(v, "seven_day_apy", "apy_7d", "yield_7d", "apy",
                            "annualized_yield_7d", "seven_day_yield", "avg_apy",
                            "validator_apy", "weekly_apy", "apy_1_week")
        apy = safe_float(apy_raw)
        if apy is not None and apy <= 5.0:
            apy = apy * 100
        rank = first_val(v, "validator_rank", "rank", "position")
        validators.append({"hotkey": hotkey, "seven_day_apy": apy, "validator_rank": rank})
    best_apy = max((v["seven_day_apy"] for v in validators if v["seven_day_apy"] is not None),
                   default=None)
    return {"netuid": netuid, "seven_day_apy": best_apy, "validators": validators}


def parse_flow(raw, netuid):
    data = raw.get("data", raw)
    if isinstance(data, list):
        matches = [x for x in data if isinstance(x, dict) and str(x.get("netuid")) == str(netuid)]
        d = matches[0] if matches else (data[0] if data else {})
    elif isinstance(data, dict):
        d = data
    else:
        d = {}
    flow_raw = safe_float(first_val(d, "tao_flow", "flow", "net_tao_in", "net_flow",
                                    "tao_net_flow", "net_tao_flow", "alpha_flow",
                                    "tao_in", "net_stake_flow", "stake_flow"))
    if flow_raw is not None:
        flow_tao = flow_raw / 1_000_000_000 if abs(flow_raw) > 1_000_000 else flow_raw
    else:
        flow_tao = None
    return {"netuid": netuid, "flow": flow_tao, "flow_ema": flow_tao}


# ── Background prefetch ───────────────────────────────────────────────────────

def _record_error(kind, netuid, exc):
    """Record a per-subnet fetch error in the cache."""
    err_msg = f"{type(exc).__name__}: {exc}"
    with _cache_lock:
        _cache["fetch_errors"][f"{kind}:{netuid}"] = err_msg
    app.logger.warning("SN%s %s error: %s", netuid, kind, err_msg)


def _clear_error(kind, netuid):
    with _cache_lock:
        _cache["fetch_errors"].pop(f"{kind}:{netuid}", None)


def fetch_subnet_data(netuid):
    """
    Fetch pool, yield, and flow data for a single subnet.
    Returns tuple of (netuid, success_count, error_count).
    All requests go through rate_limiter.
    """
    errors = 0
    
    # Pool
    try:
        raw = taostats_get(f"{TAOSTATS_BASE}/api/dtao/pool/latest/v1?netuid={netuid}")
        with _cache_lock:
            _cache["pool"][netuid] = parse_pool(raw, netuid)
            _cache["pool_updated"][netuid] = time.time()
        _clear_error("pool", netuid)
        app.logger.info("SN%s pool OK", netuid)
    except Exception as e:
        _record_error("pool", netuid, e)
        errors += 1

    # Yield (paginated)
    try:
        yield_data = []
        page = 1
        while True:
            raw = taostats_get(
                f"{TAOSTATS_BASE}/api/dtao/validator/yield/latest/v1"
                f"?netuid={netuid}&page={page}&limit=50"
            )
            page_data = raw.get("data", [])
            if isinstance(page_data, dict) and "results" in page_data:
                page_data = page_data["results"]
            if not isinstance(page_data, list):
                page_data = []
            yield_data.extend(page_data)
            pagination = raw.get("pagination", {})
            if pagination.get("next_page") is None or page >= 5:
                break
            page += 1
        with _cache_lock:
            _cache["yield"][netuid] = parse_yield(yield_data, netuid)
            _cache["yield_updated"][netuid] = time.time()
        _clear_error("yield", netuid)
        app.logger.info("SN%s yield OK (%d validators)", netuid, len(yield_data))
    except Exception as e:
        _record_error("yield", netuid, e)
        errors += 1

    # Flow
    try:
        raw = taostats_get(f"{TAOSTATS_BASE}/api/dtao/tao_flow/v1?netuid={netuid}")
        with _cache_lock:
            _cache["flow"][netuid] = parse_flow(raw, netuid)
            _cache["flow_updated"][netuid] = time.time()
        _clear_error("flow", netuid)
        app.logger.info("SN%s flow OK", netuid)
    except Exception as e:
        _record_error("flow", netuid, e)
        errors += 1
    
    return (netuid, 3 - errors, errors)


def fetch_all_data():
    """
    Fetches wallet + all subnet data sequentially.
    Per-subnet failures are recorded in fetch_errors but don't abort the cycle.
    """
    app.logger.info("Cache refresh starting (sequential mode)...")
    with _cache_lock:
        _cache["status"] = "refreshing"
        _cache["error"] = None

    try:
        # 1. Wallet (single request)
        wallet_raw = taostats_get(
            f"{TAOSTATS_BASE}/api/dtao/stake_balance/latest/v1?coldkey={COLDKEY}&limit=50"
        )
        positions = parse_wallet(wallet_raw)
        netuids = [p["netuid"] for p in positions if p["tao_value"] > 0.0001]
        with _cache_lock:
            _cache["wallet"] = positions
        app.logger.info("Wallet cached: %d positions: %s", len(netuids), netuids)

        # 2. Per-subnet data (sequential)
        total_success = 0
        total_errors = 0
        
        for netuid in netuids:
            _, successes, errors = fetch_subnet_data(netuid)
            total_success += successes
            total_errors += errors

        with _cache_lock:
            _cache["last_updated"] = time.time()
            _cache["status"] = "ready"
            err_count = len(_cache["fetch_errors"])
        
        app.logger.info(
            "Cache refresh complete. Requests: %d OK, %d failed. Outstanding errors: %d",
            total_success, total_errors, err_count
        )

    except Exception as e:
        app.logger.error("Cache refresh failed: %s", e)
        with _cache_lock:
            _cache["status"] = "error"
            _cache["error"] = str(e)


def background_loop():
    while True:
        fetch_all_data()
        time.sleep(CACHE_TTL)


_bg_thread = threading.Thread(target=background_loop, daemon=True)
_bg_thread.start()


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    response = send_from_directory("static", "index.html")
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route("/api/cache")
def get_cache():
    """Single endpoint — returns all data the frontend needs."""
    with _cache_lock:
        return jsonify({
            "status": _cache["status"],
            "error": _cache["error"],
            "last_updated": _cache["last_updated"],
            "coldkey": COLDKEY,
            "positions": _cache["wallet"] or [],
            "pool": _cache["pool"],
            "yield": _cache["yield"],
            "flow": _cache["flow"],
            "pool_updated": _cache["pool_updated"],
            "yield_updated": _cache["yield_updated"],
            "flow_updated": _cache["flow_updated"],
            "fetch_errors": _cache["fetch_errors"],
        })


@app.route("/api/cache/refresh", methods=["POST"])
def trigger_refresh():
    """Kick off a manual refresh without blocking."""
    if _cache.get("status") == "refreshing":
        return jsonify({"message": "Already refreshing"}), 202
    threading.Thread(target=fetch_all_data, daemon=True).start()
    return jsonify({"message": "Refresh started"}), 202


@app.route("/api/debug/<int:netuid>")
def debug_subnet(netuid):
    """
    Show cached data + fetch errors + cache age for a subnet.
    By default, does NOT hit the live API — that competes with the background
    refresh for the 5 req/min budget. Pass ?live=1 to opt in to a live pool call.
    """
    results = {}
    with _cache_lock:
        results["cached_pool"] = _cache["pool"].get(netuid)
        results["cached_yield"] = _cache["yield"].get(netuid)
        results["cached_flow"] = _cache["flow"].get(netuid)
        results["pool_updated"] = _cache["pool_updated"].get(netuid)
        results["yield_updated"] = _cache["yield_updated"].get(netuid)
        results["flow_updated"] = _cache["flow_updated"].get(netuid)
        results["cache_status"] = _cache["status"]
        results["cache_last_updated"] = _cache["last_updated"]
        results["fetch_errors"] = {
            k: v for k, v in _cache["fetch_errors"].items()
            if k.endswith(f":{netuid}")
        }

    # Annotate freshness
    now = time.time()
    for kind in ("pool", "yield", "flow"):
        ts = results.get(f"{kind}_updated")
        results[f"{kind}_age_minutes"] = round((now - ts) / 60, 1) if ts else None

    if request.args.get("live") == "1":
        try:
            url = f"{TAOSTATS_BASE}/api/dtao/pool/latest/v1?netuid={netuid}"
            r = requests.get(url, headers=HEADERS, timeout=10)
            body = r.json()
            d = body.get("data", body)
            if isinstance(d, list):
                d = d[0] if d else {}
            results["pool_live"] = {
                "status": r.status_code,
                "top_level_keys": list(d.keys()) if isinstance(d, dict) else [],
                "body": body,
            }
        except Exception as e:
            results["pool_live"] = {"error": str(e)}
    return jsonify(results)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
