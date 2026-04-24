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

        price = safe_float(first_val(d, "price", "last_price", "alpha_price", "token_price"))

        # price_change fields come back as full percentages (e.g. -1.53, not -0.0153)
        # Only multiply by 100 if the value looks like a decimal fraction (abs < 2)
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

        # CONFIRMED from debug: pool response does NOT have an "emission" field.
        # root_prop IS present (e.g. 0.1582) = this subnet's share of root emissions.
        # We expose it as emission_pct (multiply by 100 to get a percentage).
        emission = None
        emission_pct = None

        root_prop = safe_float(first_val(d, "emission", "emission_share", "tao_emission", "root_prop"))
        if root_prop is not None:
            # If already > 1 it was already a percentage, otherwise it's a fraction
            emission_pct = root_prop if root_prop > 1.0 else root_prop * 100
            emission = root_prop

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
    """Fetch validator APY data for a subnet.

    CONFIRMED from debug: seven_day_apy is returned as a FRACTION, not a percentage.
    e.g. 0.402 = 40.2% APY. We multiply by 100 before returning.
    """
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

            # CONFIRMED: values like 0.402 are fractions → multiply by 100 to get 40.2%
            # Guard: if somehow a value comes back > 5 it's already a percentage
            if apy is not None and apy <= 5.0:
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
    """Fetch TAO flow trend for a subnet.

    CONFIRMED from debug: response only has 'tao_flow' (no EMA field).
    tao_flow is a raw rao value (e.g. 4046653175922 = ~4046 TAO net inflow).
    Positive = net buying pressure. We use it directly as the flow signal.
    """
    try:
        url = f"{TAOSTATS_BASE}/api/dtao/tao_flow/v1?netuid={netuid}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        raw = r.json()
        data = raw.get("data", [])
        if not data:
            return jsonify({"netuid": netuid, "flow": None, "flow_ema": None})

        d = data[0] if isinstance(data, list) else data

        # CONFIRMED: only 'tao_flow' exists, no EMA variant
        flow_raw = safe_float(first_val(d,
            "tao_flow", "flow", "net_tao_in", "net_flow",
            "tao_net_flow", "net_tao_flow", "alpha_flow"
        ))

        # Convert from rao to TAO for readability in tooltips
        flow_tao = flow_raw / 1_000_000_000 if flow_raw is not None else None

        # No EMA available — use raw flow as the signal value
        # flow_ema is what the frontend checks for sign (positive = green)
        return jsonify({
            "netuid": netuid,
            "flow": flow_tao,
            "flow_ema": flow_tao,  # fallback: raw flow used as signal
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
