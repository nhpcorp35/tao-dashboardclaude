import os
import requests
from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS

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
    """Fetch real staked positions for the configured coldkey."""
    try:
        url = f"{TAOSTATS_BASE}/api/dtao/stake_balance/latest/v1?coldkey={COLDKEY}&limit=50"
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        positions = []
        for item in data.get("data", []):
            netuid = item.get("netuid")
            if netuid is None:
                continue
            balance_rao = int(item.get("balance", 0))
            balance_tao = balance_rao / 1_000_000_000
            balance_as_tao_rao = int(item.get("balance_as_tao", 0))
            balance_as_tao = balance_as_tao_rao / 1_000_000_000
            positions.append({
                "netuid": netuid,
                "hotkey": item.get("hotkey", {}).get("ss58", ""),
                "hotkey_name": item.get("hotkey_name", ""),
                "alpha_balance": round(balance_tao, 6),
                "tao_value": round(balance_as_tao, 6),
                "subnet_rank": item.get("subnet_rank"),
            })
        return jsonify({"coldkey": COLDKEY, "positions": positions})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/subnet/<int:netuid>")
def subnet_pool(netuid):
    """Fetch alpha price + 7d change for a subnet."""
    try:
        url = f"{TAOSTATS_BASE}/api/dtao/pool/latest/v1?netuid={netuid}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        raw = r.json()
        d = raw.get("data", [])
        d = d[0] if isinstance(d, list) and d else d
        price = float(d.get("price", 0))
        change_7d = d.get("price_change_1_week") or d.get("price_change_7d")
        change_7d = float(change_7d) if change_7d is not None else None
        emission = d.get("emission")
        return jsonify({
            "netuid": netuid,
            "price": price,
            "change_7d": change_7d,
            "emission": emission,
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
