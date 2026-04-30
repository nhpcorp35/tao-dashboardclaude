import os
import time
import threading
import requests
from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

app = Flask(__name__, static_folder="static")
CORS(app)

TAOSTATS_API_KEY = os.environ.get("TAOSTATS_API_KEY", "")
TAOSTATS_BASE = "https://api.taostats.io"
COLDKEY = os.environ.get("COLDKEY", "5Cexeg7deNSTzsqMKuBmvc9JHGHymuL4SdjAA9Jw4eeHUphb")
HEADERS = {"Authorization": TAOSTATS_API_KEY}

# Rate limit: 60 req/min on Standard tier
REQUEST_GAP = 1
CACHE_TTL = 10 * 60  # Refresh every 10 minutes to stay under rate limits
MAX_RETRIES = 3
RETRY_BACKOFF = 10
COOLDOWN_MINUTES = 5

# Rate limit cooldown tracking
rate_limit_cooldown_until = None
cooldown_lock = threading.Lock()


def set_rate_limit_cooldown():
    """Set cooldown for COOLDOWN_MINUTES after hitting 429."""
    global rate_limit_cooldown_until
    with cooldown_lock:
        rate_limit_cooldown_until = datetime.now() + timedelta(minutes=COOLDOWN_MINUTES)
        app.logger.warning(
            "Rate limit cooldown activated until %s (%d minutes)",
            rate_limit_cooldown_until.strftime("%H:%M:%S"),
            COOLDOWN_MINUTES
        )


def get_cooldown_status():
    """Check if we're in cooldown. Returns (is_cooling_down, seconds_remaining)."""
    global rate_limit_cooldown_until
    with cooldown_lock:
        if rate_limit_cooldown_until is None:
            return (False, 0)
        
        now = datetime.now()
        if now >= rate_limit_cooldown_until:
            rate_limit_cooldown_until = None
            app.logger.info("Rate limit cooldown expired - resuming refreshes")
            return (False, 0)
        
        seconds_left = int((rate_limit_cooldown_until - now).total_seconds())
        return (True, seconds_left)


# In-memory cache
_cache = {
    "wallet": None,
    "pool": {},
    "yield": {},
    "flow": {},
    "tao_price": None,
    "pool_updated": {},
    "yield_updated": {},
    "flow_updated": {},
    "last_updated": None,
    "status": "pending",
    "error": None,
    "fetch_errors": {},
    "cooldown_until": None,
}
_cache_lock = threading.Lock()


# Rate Limiter
class RateLimiter:
    """Global rate limiter ensuring no more than 60 requests per minute."""
    def __init__(self, requests_per_minute):
        self.min_gap = 60.0 / requests_per_minute
        self.last_request_time = 0
        self.lock = threading.Lock()
    
    def wait_and_acquire(self):
        """Block until enough time has passed since last request."""
        with self.lock:
            now = time.time()
            elapsed = now - self.last_request_time
            if elapsed < self.min_gap:
                wait_time = self.min_gap - elapsed
                time.sleep(wait_time)
            self.last_request_time = time.time()

rate_limiter = RateLimiter(60)


# Helpers
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
    """GET against TaoStats with retry and cooldown on 429."""
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            rate_limiter.wait_and_acquire()
            
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 429:
                if attempt < max_retries:
                    wait = RETRY_BACKOFF * (2 ** attempt)
                    app.logger.warning(
                        "Rate limited on %s - backing off %ds (attempt %d/%d)",
                        url, wait, attempt + 1, max_retries
                    )
                    time.sleep(wait)
                    continue
                
                set_rate_limit_cooldown()
                raise requests.HTTPError(f"429 Rate Limited after {max_retries} retries - cooldown activated")
            
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last_err = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            retryable = status is None or status >= 500
            if retryable and attempt < max_retries:
                wait = RETRY_BACKOFF * (2 ** attempt)
                app.logger.warning(
                    "Request error on %s: %s - retrying in %ds (attempt %d/%d)",
                    url, e, wait, attempt + 1, max_retries
                )
                time.sleep(wait)
                continue
            raise
    raise last_err


# Data parsers
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
    top_3_apy = [v["seven_day_apy"] for v in sorted(validators, key=lambda x: x.get("seven_day_apy") or 0, reverse=True)[:3]]
    avg_7d = sum(a for a in top_3_apy if a is not None) / len(top_3_apy) if top_3_apy else None
    return {"netuid": netuid, "seven_day_apy": avg_7d, "validators": validators}


def parse_flow(raw, netuid):
    d = raw.get("data", raw)
    if isinstance(d, list):
        matches = [x for x in d if isinstance(x, dict) and str(x.get("netuid")) == str(netuid)]
        d = matches[0] if matches else (d[0] if d else {})
    elif not isinstance(d, dict):
        d = {}
    flow_ema = safe_float(first_val(d, "flow_ema", "tao_flow_ema", "flow", "tao_flow",
                                    "flow_moving_average", "ema_flow"))
    return {"netuid": netuid, "flow_ema": flow_ema, "flow": flow_ema}


def fetch_tao_price():
    """Fetch TAO/USD price from CoinGecko."""
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bittensor&vs_currencies=usd"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data.get("bittensor", {}).get("usd")
    except Exception as e:
        app.logger.warning("Failed to fetch TAO price: %s", e)
        return None


def _record_error(kind, netuid, exc):
    """Store last error for a (kind, netuid) pair."""
    key = f"{kind}:{netuid}"
    msg = f"{exc.__class__.__name__}: {exc}"
    with _cache_lock:
        _cache["fetch_errors"][key] = msg
    app.logger.error("SN%s %s error: %s", netuid, kind, msg)


def _clear_error(kind, netuid):
    """Clear recorded error on success."""
    key = f"{kind}:{netuid}"
    with _cache_lock:
        _cache["fetch_errors"].pop(key, None)


def fetch_subnet_data(netuid):
    """Fetch pool + yield + flow for one subnet."""
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

    # Yield
    try:
        yield_data = []
        page = 1
        while True:
            raw = taostats_get(
                f"{TAOSTATS_BASE}/api/dtao/pool_validators/latest/v1?netuid={netuid}&page={page}&page_size=100"
            )
            page_data = raw.get("data", raw)
            if isinstance(page_data, dict) and "results" in page_data:
                page_data = page_data["results"]
            if not isinstance(page_data, list):
                page_data = []
            yield_data.extend(page_data)
            pagination = raw.get("pagination", {})
            if pagination.get("next_page") is None or page >= 1:  # Only fetch first 100 validators
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
    """Fetch wallet + all subnet data with parallel processing."""
    # Check cooldown status
    is_cooling_down, seconds_left = get_cooldown_status()
    
    if is_cooling_down:
        app.logger.info("Skipping refresh - in cooldown for %d more seconds", seconds_left)
        with _cache_lock:
            _cache["status"] = "cooldown"
            _cache["cooldown_until"] = (datetime.now() + timedelta(seconds=seconds_left)).isoformat()
        return
    
    app.logger.info("Cache refresh starting (parallel mode with 5 workers)...")
    with _cache_lock:
        _cache["status"] = "refreshing"
        _cache["error"] = None
        _cache["cooldown_until"] = None

    try:
        # Wallet
        wallet_raw = taostats_get(
            f"{TAOSTATS_BASE}/api/dtao/stake_balance/latest/v1?coldkey={COLDKEY}&limit=50"
        )
        positions = parse_wallet(wallet_raw)
        netuids = [p["netuid"] for p in positions if p["tao_value"] > 0.0001]
        with _cache_lock:
            _cache["wallet"] = positions
        app.logger.info("Wallet cached: %d positions: %s", len(netuids), netuids)

        # TAO price
        tao_price = fetch_tao_price()
        with _cache_lock:
            _cache["tao_price"] = tao_price
        app.logger.info("TAO price: $%s", tao_price if tao_price else "N/A")

        # Per-subnet data (parallel)
        total_success = 0
        total_errors = 0
        
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(fetch_subnet_data, netuid): netuid 
                      for netuid in netuids}
            
            for future in as_completed(futures):
                netuid = futures[future]
                try:
                    _, successes, errors = future.result()
                    total_success += successes
                    total_errors += errors
                except Exception as e:
                    app.logger.error("SN%s worker failed: %s", netuid, e)
                    total_errors += 3

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


# API routes
@app.route("/")
def index():
    response = send_from_directory("static", "index.html")
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route("/api/cache")
def get_cache():
    """Single endpoint - returns all data the frontend needs."""
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
            "tao_price": _cache["tao_price"],
            "pool_updated": _cache["pool_updated"],
            "yield_updated": _cache["yield_updated"],
            "flow_updated": _cache["flow_updated"],
            "fetch_errors": _cache["fetch_errors"],
            "cooldown_until": _cache["cooldown_until"],
        })


@app.route("/api/cache/refresh", methods=["POST"])
def trigger_refresh():
    """Kick off a manual refresh without blocking."""
    is_cooling_down, seconds_left = get_cooldown_status()
    
    if is_cooling_down:
        return jsonify({
            "message": f"Rate limited - try again in {seconds_left} seconds",
            "cooldown_until": (datetime.now() + timedelta(seconds=seconds_left)).isoformat()
        }), 429
    
    if _cache.get("status") == "refreshing":
        return jsonify({"message": "Already refreshing"}), 202
    
    threading.Thread(target=fetch_all_data, daemon=True).start()
    return jsonify({"message": "Refresh started"}), 202


@app.route("/api/debug/<int:netuid>")
def debug_subnet(netuid):
    """Show cached data + fetch errors + cache age for a subnet."""
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
