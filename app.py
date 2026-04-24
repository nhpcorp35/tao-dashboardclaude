import os
import requests
from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
from collections import defaultdict

app = Flask(__name__, static_folder="static")
CORS(app)

TAOSTATS_API_KEY = os.environ.get("TAOSTATS_API_KEY", "")
TAOSTATS_BASE = "https://api.taostats.io"
COLDKEY = os.environ.get("COLDKEY", "5Cexeg7deNSTzsqMKuBmvc9JHGHymuL4SdjAA9Jw4eeHUphb")

HEADERS = {"Authorization": TAOSTATS_API_KEY}


def first_val(d, *keys, default=None):
    """Return the first non-None value found in dict d for the given keys."""
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


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# ── Debug endpoint — visit /api/debug/<netuid> to inspect raw API responses ──

@app.route("/api/debug/<int:netuid>")
def debug_subnet(netuid):
    """Returns raw API responses for a subnet so you can find correct field names."""
    results = {}

    try:
        url = f"{TAOSTATS_BASE}/api/dtao/pool/latest/v1?netuid={netuid}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        results["pool"] = {"status": r.status_code, "body": r.json()}
    except Exception as e:
        results["pool"] = {"error": str(e)}

    try:
        url = f"{TAOSTATS_BASE}/api/dtao/validator/yield/latest/v1?netuid={netuid}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        results["yield"] = {"status": r.status_code, "body": r.json()}
    except Exception as e:
        results["yield"] = {"error": str(e)}

    try:
        url = f"{TAOSTATS_BASE}/api/dtao/tao_flow/v1?netuid={netuid}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        results["tao_flow"] = {"status": r.status_code, "body": r.json()}
    except Exception as e:
        results["tao_flow"] = {"error": str(e)}

    try:
        url = f"{TAOSTATS_BASE}/api/dtao/stake_balance/latest/v1?coldkey={COLDKEY}&netuid={netuid}&limit=50"
        r = requests.get(url, headers=HEADERS, timeout=10)
        results["stake_balance"] = {"status": r.status_code, "body": r.json()}
    except Exception as e:
        results["stake_balance"] = {"error": str(e)}

    return jsonify(results)


@app.route("/api/wallet")
def wallet():
    """Fetch real staked positions grouped by subnet, with per-validator detail."""
    try:
        url = f"{TAOSTATS_BASE}/api/dtao/stake_balance/latest/v1?coldkey={COLDKEY}&limit=50"
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()

        grouped = defaultdict(lambda: {
            "netuid": None,
            "alpha_balance": 0.0,
            "tao_value": 0.0,
            "validators": [],
        })

        for item in data.get("data", []):
            netuid = item.get("netuid")
            if netuid is None:
                continue
            balance_rao = int(item.get("balance", 0))
            balance_tao = balance_rao / 1_000_000_000
            balance_as_tao_rao = int(item.get("balance_as_tao", 0))
            balance_as_tao = balance_as_tao_rao / 1_000_000_000

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

        positions = sorted(grouped.values(), key=lambda x: x["tao_value"], reverse=True)
        return jsonify({"coldkey": COLDKEY, "positions": positions})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/subnet/<int:netuid>")
def subnet_pool(netuid):
    """Fetch pool data (price, 7d/30d change, emission) for a subnet."""
    try:
        url = f"{TAOSTATS_BASE}/api/dtao/pool/latest/v1?netuid={netuid}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        raw = r.json()
        d = raw.get("data", [])
        d = d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else {})

        price = safe_float(first_val(d, "price", "alpha_price", "token_price"))

        # 7d change — may be decimal fraction (0.05) or already pct (5.0)
        change_7d = safe_float(first_val(d,
            "price_change_1_week", "price_change_7d", "change_7d",
            "token_price_change_1_week", "alpha_price_change_1_week"
        ))
        if change_7d is not None and -2.0 < change_7d < 2.0 and change_7d != 0:
            change_7d = change_7d * 100

        change_30d = safe_float(first_val(d,
            "price_change_1_month", "price_change_30d", "change_30d",
            "price_change_4_weeks", "token_price_change_1_month", "alpha_price_change_1_month"
        ))
        if change_30d is not None and -2.0 < change_30d < 2.0 and change_30d != 0:
            change_30d = change_30d * 100

        # Emission — taostats typically returns as fraction (0.003 = 0.3%)
        emission = safe_float(first_val(d, "emission", "emission_share", "tao_emission"))
        if emission is not None:
            emission_pct = emission if emission > 1.0 else emission * 100
        else:
            emission_pct = None

        return jsonify({
            "netuid": netuid,
            "price": price,
            "change_7d": change_7d,
            "change_30d": change_30d,
            "emission": emission,
            "emission_pct": emission_pct,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/yield/<int:netuid>")
def validator_yield(netuid):
    """Fetch validator APY data for a subnet."""
    try:
        url = f"{TAOSTATS_BASE}/api/dtao/validator/yield/latest/v1?netuid={netuid}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        raw = r.json()
        data = raw.get("data", [])
        if not data:
            return jsonify({"netuid": netuid, "seven_day_apy": None, "validators": []})

        validators = []
        for v in data:
            hotkey_obj = v.get("hotkey", {})
            hotkey = hotkey_obj.get("ss58", "") if isinstance(hotkey_obj, dict) else str(hotkey_obj)

            apy_raw = first_val(v,
                "seven_day_apy", "apy_7d", "yield_7d", "apy",
                "annualized_yield_7d", "seven_day_yield", "avg_apy"
            )
            apy = safe_float(apy_raw)
            # Convert fraction to pct if needed
            if apy is not None and 0 < apy < 2.0:
                apy = apy * 100

            rank = first_val(v, "validator_rank", "rank", "position")

            validators.append({
                "hotkey": hotkey,
                "seven_day_apy": apy,
                "validator_rank": rank,
            })

        best_apy = max(
            (v["seven_day_apy"] for v in validators if v["seven_day_apy"] is not None),
            default=None
        )

        return jsonify({
            "netuid": netuid,
            "seven_day_apy": best_apy,
            "validators": validators,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/taoflow/<int:netuid>")
def tao_flow(netuid):
    """Fetch TAO flow trend for a subnet."""
    try:
        url = f"{TAOSTATS_BASE}/api/dtao/tao_flow/v1?netuid={netuid}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        raw = r.json()
        data = raw.get("data", [])
        if not data:
            return jsonify({"netuid": netuid, "flow": None, "flow_ema": None})

        d = data[0] if isinstance(data, list) else data

        flow = safe_float(first_val(d,
            "tao_flow", "flow", "net_tao_in", "net_flow",
            "tao_net_flow", "net_tao_flow", "alpha_flow"
        ))
        flow_ema = safe_float(first_val(d,
            "tao_flow_ema", "flow_ema", "tao_in_ema", "net_flow_ema",
            "ema_flow", "tao_flow_ma", "flow_ma"
        ))

        # Fall back to raw flow for the signal if EMA not available
        if flow_ema is None:
            flow_ema = flow

        return jsonify({
            "netuid": netuid,
            "flow": flow,
            "flow_ema": flow_ema,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/subnets")
def all_subnets():
    """Fetch top-level subnet list with emissions and prices."""
    try:
        url = f"{TAOSTATS_BASE}/api/subnet/latest/v1"
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
