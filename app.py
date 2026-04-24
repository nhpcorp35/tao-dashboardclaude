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


@app.route("/api/debug/<int:netuid>")
def debug_subnet(netuid):
    results = {}
    try:
        url = f"{TAOSTATS_BASE}/api/dtao/pool/latest/v1?netuid={netuid}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        body = r.json()
        d = body.get("data", body)
        if isinstance(d, list): d = d[0] if d else {}
        results["pool"] = {"status": r.status_code, "body": body, "top_level_keys": list(d.keys())}
    except Exception as e:
        results["pool"] = {"error": str(e)}
    try:
        url = f"{TAOSTATS_BASE}/api/dtao/validator/yield/latest/v1?netuid={netuid}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        body = r.json()
        d0 = (body.get("data") or [{}])
        d0 = d0[0] if isinstance(d0, list) and d0 else (d0 if isinstance(d0, dict) else {})
        results["yield"] = {"status": r.status_code, "body": body, "first_record_keys": list(d0.keys())}
    except Exception as e:
        results["yield"] = {"error": str(e)}
    try:
        url = f"{TAOSTATS_BASE}/api/dtao/tao_flow/v1?netuid={netuid}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        body = r.json()
        d0 = (body.get("data") or [{}])
        d0 = d0[0] if isinstance(d0, list) and d0 else (d0 if isinstance(d0, dict) else {})
        results["tao_flow"] = {"status": r.status_code, "body": body, "first_record_keys": list(d0.keys())}
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
    try:
        url = f"{TAOSTATS_BASE}/api/dtao/pool/latest/v1?netuid={netuid}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        raw = r.json()

        # Robust unwrap: handle list, dict-with-data, or bare dict
        d = raw.get("data", raw)
        if isinstance(d, list):
            # Filter to the exact netuid in case the API returns multiple rows
            matches = [x for x in d if isinstance(x, dict) and str(x.get("netuid")) == str(netuid)]
            d = matches[0] if matches else (d[0] if d else {})
        elif not isinstance(d, dict):
            d = {}

        # Name — TaoStats may nest it under subnet.name or use various keys
        subnet_obj = d.get("subnet") or {}
        name = (
            first_val(d, "name", "subnet_name", "title", "token_name", "symbol")
            or first_val(subnet_obj, "name", "subnet_name", "title")
        )

        price = safe_float(first_val(d,
            "price", "last_price", "alpha_price", "token_price",
            "current_price", "alpha_token_price",
        ))

        # CONFIRMED: taostats returns price changes as full percentages already (e.g. -1.53)
        # Do not multiply by 100
        change_7d = safe_float(first_val(d,
            "price_change_1_week", "price_change_7d", "change_7d",
            "token_price_change_1_week", "alpha_price_change_1_week",
            "alpha_price_change_7d", "price_change_week",
        ))
        change_30d = safe_float(first_val(d,
            "price_change_1_month", "price_change_30d", "change_30d",
            "price_change_4_weeks", "token_price_change_1_month", "alpha_price_change_1_month",
            "alpha_price_change_30d", "price_change_month",
        ))

        # CONFIRMED: field is root_prop, value is a fraction (0.158 = 15.8%)
        emission = None
        emission_pct = None
        root_prop = safe_float(first_val(d,
            "root_prop", "emission", "emission_share", "tao_emission",
            "emission_ratio", "tao_emission_ratio",
        ))
        if root_prop is not None:
            emission_pct = root_prop if root_prop > 1.0 else root_prop * 100
            emission = root_prop

        # Log what we found so mismatches are easy to spot in server logs
        app.logger.info(
            "subnet %s pool keys=%s price=%s change_7d=%s emission_pct=%s",
            netuid, list(d.keys())[:20], price, change_7d, emission_pct,
        )

        return jsonify({
            "netuid": netuid,
            "name": name,
            "price": price,
            "change_7d": change_7d,
            "change_30d": change_30d,
            "emission": emission,
            "emission_pct": emission_pct,
            # Pass raw keys back so the debug endpoint still works
            "_raw_keys": list(d.keys()),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/yield/<int:netuid>")
def validator_yield(netuid):
    try:
        url = f"{TAOSTATS_BASE}/api/dtao/validator/yield/latest/v1?netuid={netuid}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        raw = r.json()

        data = raw.get("data", raw)
        if isinstance(data, dict) and "results" in data:
            data = data["results"]
        if not isinstance(data, list):
            data = [data] if isinstance(data, dict) and data else []

        if not data:
            app.logger.info("yield netuid=%s: empty data, raw keys=%s", netuid, list(raw.keys()))
            return jsonify({"netuid": netuid, "seven_day_apy": None, "validators": []})

        validators = []
        for v in data:
            hotkey_obj = v.get("hotkey", {})
            hotkey = hotkey_obj.get("ss58", "") if isinstance(hotkey_obj, dict) else str(hotkey_obj)

            apy_raw = first_val(v,
                "seven_day_apy", "apy_7d", "yield_7d", "apy",
                "annualized_yield_7d", "seven_day_yield", "avg_apy",
                "validator_apy", "weekly_apy", "apy_1_week",
            )
            apy = safe_float(apy_raw)
            # CONFIRMED: values are fractions (0.402 = 40.2%) — multiply by 100
            # Guard: if already > 5 assume it's already a percentage
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
    try:
        url = f"{TAOSTATS_BASE}/api/dtao/tao_flow/v1?netuid={netuid}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        raw = r.json()

        # Handle both {data: [...]} and bare list/dict
        data = raw.get("data", raw)
        if isinstance(data, list):
            # Prefer the row matching our netuid
            matches = [x for x in data if isinstance(x, dict) and str(x.get("netuid")) == str(netuid)]
            d = matches[0] if matches else (data[0] if data else {})
        elif isinstance(data, dict):
            d = data
        else:
            d = {}

        if not d:
            app.logger.info("tao_flow netuid=%s: empty data, raw keys=%s", netuid, list(raw.keys()))
            return jsonify({"netuid": netuid, "flow": None, "flow_ema": None})

        flow_raw = safe_float(first_val(d,
            "tao_flow", "flow", "net_tao_in", "net_flow",
            "tao_net_flow", "net_tao_flow", "alpha_flow",
            "tao_in", "net_stake_flow", "stake_flow",
        ))

        # Some endpoints return rao (integer) and need /1e9; others return TAO directly.
        # Heuristic: if abs value > 1_000_000 it's almost certainly rao.
        if flow_raw is not None:
            flow_tao = flow_raw / 1_000_000_000 if abs(flow_raw) > 1_000_000 else flow_raw
        else:
            flow_tao = None
            app.logger.info(
                "tao_flow netuid=%s: no flow field found in keys=%s", netuid, list(d.keys())
            )

        return jsonify({
            "netuid": netuid,
            "flow": flow_tao,
            "flow_ema": flow_tao,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/subnets")
def all_subnets():
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
