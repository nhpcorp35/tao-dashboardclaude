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


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


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
            g["validators"].append({
                "hotkey": item.get("hotkey", {}).get("ss58", ""),
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
        d = d[0] if isinstance(d, list) and d else d

        price = float(d.get("price", 0)) if d.get("price") else None
        change_7d = d.get("price_change_1_week")
        change_7d = float(change_7d) if change_7d is not None else None
        change_30d = d.get("price_change_1_month") or d.get("price_change_30d") or d.get("price_change_4_weeks")
        change_30d = float(change_30d) if change_30d is not None else None
        emission = d.get("emission")
        emission_pct = float(emission) * 100 if emission is not None else None

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

        # Return best APY across validators + per-validator APY
        validators = []
        for v in data:
            hotkey = v.get("hotkey", {}).get("ss58", "") if isinstance(v.get("hotkey"), dict) else v.get("hotkey", "")
            validators.append({
                "hotkey": hotkey,
                "seven_day_apy": v.get("seven_day_apy"),
                "validator_rank": v.get("validator_rank") or v.get("rank"),
            })

        best_apy = max((float(v["seven_day_apy"]) for v in validators if v["seven_day_apy"] is not None), default=None)

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
        return jsonify({
            "netuid": netuid,
            "flow": d.get("tao_flow") or d.get("flow"),
            "flow_ema": d.get("tao_flow_ema") or d.get("flow_ema"),
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
