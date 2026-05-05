# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════╗
║          SOJUU BOT — CANTEX V2                       ║
║          Automated Swap Bot for Cantex DEX           ║
║          Canton Network — MAINNET                    ║
║                                                      ║
║  Multi-account · Dynamic pair resolution             ║
║  Network fee guard · Slippage protection             ║
║                                                      ║
║  © 2026 Sojuu Community                              ║
╚══════════════════════════════════════════════════════╝

Run:      python server.py
Frontend: http://localhost:5001
"""
# ── Imports ──────────────────────────────────────────────────────
import asyncio
import random
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

from flask import Flask, Response, jsonify, request, send_from_directory
from flask_cors import CORS

try:
    from cantex_sdk import (
        CantexSDK, InstrumentId, OperatorKeySigner, IntentTradingKeySigner,
        CantexAPIError, CantexAuthError, CantexTimeoutError, CantexError,
        SwapExecutedEvent, SwapFailedEvent, SwapPendingEvent,
    )
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False
    InstrumentId = type("InstrumentId", (), {})
    print("WARNING: cantex_sdk not installed. Running in DEMO mode.")

# ── Identity ─────────────────────────────────────────────────────
BOT_INFO = {
    "name":    "Sojuu Bot",
    "product": "Cantex V2",
    "version": "2.0.0",
    "network": "Canton Mainnet",
    "credit":  "© 2026 Sojuu Community",
}

# ── Bot Mode ─────────────────────────────────────────────────────
# "normal"    : mode biasa (CC ↔ USDCx, 2 arah)
# "sell_only" : hanya sell CC → USDCx, 130 CC fixed per tx
BOT_MODE           = "normal"   # diubah lewat CLI saat startup
SELL_ONLY_AMOUNT   = Decimal("130")  # CC fixed per transaksi sell-only

# ── App ──────────────────────────────────────────────────────────
app = Flask(__name__, static_folder='.')
CORS(app)

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S')
log = logging.getLogger('CantexBot')

# ── Paths & Config ───────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
LOG_FILE    = os.path.join(BASE_DIR, 'swap_log.jsonl')
AUTH_FILE   = os.path.join(BASE_DIR, 'auth.json')

# ── Supported Pairs ──────────────────────────────────────────────
SUPPORTED_PAIRS = {
    "CC/USDCx":   ("Amulet", "USDCx"),
    "cBTC/USDCx": ("cBTC",   "USDCx"),
}

# ── Default Config ───────────────────────────────────────────────
DEFAULT_CONFIG = {
    "base_url":           "https://api.cantex.io",
    "swap_amount_a":      100.0,
    "use_full_b":         True,
    "swap_amount_b":      100.0,
    "max_network_fee":    0.15,
    "fee_check_interval": 5,
    "delay_a_to_b":       30,
    "delay_per_loop":     600,
    "max_slippage":       0.05,
    "confirm_timeout":    120.0,
}

# ── Auth ─────────────────────────────────────────────────────────
_auth = {
    "username":      "",
    "password_hash": "",
    "session_ttl":   8 * 3600,
}
_sessions = {}

def _hash_password(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()

def load_auth():
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE) as f:
                data = json.load(f)
            _auth["username"]      = data.get("username", "")
            _auth["password_hash"] = data.get("passwordHash", "")
        except Exception as e:
            log.warning(f"Auth load error: {e}")

def save_auth(username: str, password_hash: str):
    with open(AUTH_FILE, 'w') as f:
        json.dump({"username": username, "passwordHash": password_hash}, f, indent=2)
    _auth["username"]      = username
    _auth["password_hash"] = password_hash
    log.info(f"Auth saved — username: {username}")

def _get_session_id() -> str | None:
    return request.cookies.get("cantex_sid")

def is_authenticated() -> bool:
    sid = _get_session_id()
    if not sid:
        return False
    created_at = _sessions.get(sid)
    if created_at is None:
        return False
    if (time.time() - created_at) > _auth["session_ttl"]:
        _sessions.pop(sid, None)
        return False
    return True

def require_auth():
    return jsonify({"ok": False, "message": "Unauthorized — login required"}), 401

# ── Shared State ─────────────────────────────────────────────────
_lock = threading.Lock()
state = {
    "config":      {},
    "accounts":    [],
    "bot_loop":    None,
    "bot_tasks":   {},
    "start_time":  time.time(),
    "swap_count":  0,
    "sse_clients": [],
}

# ── Persistence ──────────────────────────────────────────────────
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                saved = json.load(f)
            cfg.update(saved)
        except Exception as e:
            log.warning(f"Config load error: {e}")
    state["config"] = cfg

def save_config():
    with open(CONFIG_FILE, 'w') as f:
        json.dump(state["config"], f, indent=2)

def load_accounts():
    accs_file = os.path.join(BASE_DIR, 'accounts.json')
    if os.path.exists(accs_file):
        try:
            with open(accs_file) as f:
                saved = json.load(f)
            for a in saved:
                a.setdefault('status',       'stopped')
                a.setdefault('phase',        '—')
                a.setdefault('balA',         '—')
                a.setdefault('balB',         '—')
                a.setdefault('pair',         'CC/USDCx')
                a.setdefault('swap_count',   0)
                a.setdefault('last_msg',     '')
                a.setdefault('is_paused',    False)
                a.setdefault('network_fee',  '—')
            state["accounts"] = saved
        except Exception as e:
            log.warning(f"Accounts load error: {e}")

def save_accounts():
    accs_file = os.path.join(BASE_DIR, 'accounts.json')
    to_save = []
    for a in state["accounts"]:
        to_save.append({
            "name":         a.get("name"),
            "operator_key": a.get("operator_key"),
            "trading_key":  a.get("trading_key"),
            "pair":         a.get("pair", "CC/USDCx"),
        })
    with open(accs_file, 'w') as f:
        json.dump(to_save, f, indent=2)

# ── Swap Log ─────────────────────────────────────────────────────
def write_log(entry: dict):
    entry['utc'] = datetime.now(timezone.utc).isoformat()
    with open(LOG_FILE, 'a') as f:
        f.write(json.dumps(entry) + '\n')
    with _lock:
        state["swap_count"] += 1

def read_logs(page=1, limit=25):
    if not os.path.exists(LOG_FILE):
        return [], 0
    with open(LOG_FILE) as f:
        lines = [l.strip() for l in f.readlines() if l.strip()]
    lines.reverse()
    total = len(lines)
    start = (page - 1) * limit
    items = []
    for l in lines[start:start + limit]:
        try:
            items.append(json.loads(l))
        except Exception:
            pass
    return items, total


def cc_daily_history(days: int = 30) -> list[dict]:
    """
    Compute per-day CC net change from swap_log.jsonl.

    Each successful swap entry contains:
      action  — e.g. "Amulet→USDCx"  (CC sold, USDCx received)
                    or "USDCx→Amulet" (USDCx sold, CC received)
      amount  — how much was *sold*
      out     — how much was *received*

    CC-related tokens: "Amulet", "CC" (both represent the CC token on Cantex)
    USDCx-related token: "USDCx"

    Returns a list OrderedDict per day (ascending), each with:
      date        — "YYYY-MM-DD"
      cc_in       — CC received (from USDCx→CC swaps)
      cc_out      — CC sold (from CC→USDCx swaps)
      net         — cc_in - cc_out   (positive = gained CC)
      swap_count  — number of successful swaps that day
    """
    if not os.path.exists(LOG_FILE):
        return []

    CC_TOKENS = {"amulet", "cc"}

    from collections import OrderedDict
    day_data: dict[str, dict] = OrderedDict()

    with open(LOG_FILE) as f:
        lines = [l.strip() for l in f.readlines() if l.strip()]

    for line in lines:
        try:
            e = json.loads(line)
        except Exception:
            continue

        if e.get("status") != "SUCCESS":
            continue

        utc_str = e.get("utc", "")
        try:
            day = utc_str[:10]   # "YYYY-MM-DD"
        except Exception:
            continue

        if not day:
            continue

        action = e.get("action", "")
        # action format: "TokenA→TokenB"
        parts = action.replace("→", "->").split("->")
        if len(parts) != 2:
            continue

        sell_tok = parts[0].strip().lower()
        buy_tok  = parts[1].strip().lower()

        try:
            amount = float(e.get("amount", 0))
            out    = float(e.get("out",    0))
        except Exception:
            continue

        if day not in day_data:
            day_data[day] = {"date": day, "cc_in": 0.0, "cc_out": 0.0, "net": 0.0, "swap_count": 0}

        d = day_data[day]
        d["swap_count"] += 1

        if sell_tok in CC_TOKENS:
            # sold CC → out is USDCx
            d["cc_out"] += amount
        elif buy_tok in CC_TOKENS:
            # sold USDCx → out is CC
            d["cc_in"] += out

    # compute net and round
    result = []
    for d in day_data.values():
        d["cc_in"]  = round(d["cc_in"],  6)
        d["cc_out"] = round(d["cc_out"], 6)
        d["net"]    = round(d["cc_in"] - d["cc_out"], 6)
        result.append(d)

    # sort ascending, return last `days`
    result.sort(key=lambda x: x["date"])
    return result[-days:]

# ── SSE ──────────────────────────────────────────────────────────
def sse_push(data: dict):
    msg = f"data: {json.dumps(data)}\n\n"
    dead = []
    for q in state["sse_clients"]:
        try:
            q.put_nowait(msg)
        except Exception:
            dead.append(q)
    for q in dead:
        try:
            state["sse_clients"].remove(q)
        except Exception:
            pass

def broadcast_state():
    with _lock:
        accounts_data = []
        for a in state["accounts"]:
            accounts_data.append({
                "name":        a.get("name", "—"),
                "operatorKey": (a.get("operator_key") or "")[:12] + "...",
                "pair":        a.get("pair", "CC/USDCx"),
                "status":      a.get("status", "stopped"),
                "phase":       a.get("phase", "—"),
                "balA":        a.get("balA", "—"),
                "balB":        a.get("balB", "—"),
                "swapCount":   a.get("swap_count", 0),
                "lastMsg":     a.get("last_msg", ""),
                "isPaused":    a.get("is_paused", False),
                "networkFee":  a.get("network_fee", "—"),
            })

        total_swaps = sum(a.get("swap_count", 0) for a in state["accounts"])
        active      = sum(1 for a in state["accounts"] if a.get("status") in ("running", "waiting_fee"))
        is_running  = bool(state["bot_tasks"])

        payload = {
            "type": "update",
            "bot": {
                "isRunning": is_running,
                "accounts":  accounts_data,
            },
            "accounts": accounts_data,
            "stats": {
                "totalAccounts":  len(state["accounts"]),
                "activeAccounts": active,
                "totalSwaps":     total_swaps,
                "totalSuccess":   total_swaps,
                "totalFailed":    0,
            }
        }
    sse_push(payload)

# ── Bot Engine ───────────────────────────────────────────────────
def _get_account(name: str) -> dict | None:
    for a in state["accounts"]:
        if a.get("name") == name:
            return a
    return None

def _set_account(name: str, **kwargs):
    with _lock:
        for a in state["accounts"]:
            if a.get("name") == name:
                a.update(kwargs)
                return

def _fmt(amount: Decimal, symbol: str = "") -> str:
    s = f"{amount:.6f}".rstrip("0").rstrip(".")
    return f"{s} {symbol}".strip() if symbol else s


def _resolve_pair(pools_info, pair: str):
    if pair not in SUPPORTED_PAIRS:
        raise ValueError(
            f"Unsupported pair '{pair}'. "
            f"Supported: {list(SUPPORTED_PAIRS.keys())}"
        )
    id_a, id_b = SUPPORTED_PAIRS[pair]

    for pool in pools_info.pools:
        if pool.token_a.id == id_a and pool.token_b.id == id_b:
            return pool.token_a, pool.token_b
        if pool.token_a.id == id_b and pool.token_b.id == id_a:
            return pool.token_b, pool.token_a

    raise ValueError(
        f"No pool found for pair '{pair}' ({id_a}/{id_b}). "
        f"Available pools: "
        + ", ".join(f"{p.token_a.id}/{p.token_b.id}" for p in pools_info.pools)
    )


async def run_account_bot_sell_only(acc: dict):
    """
    Mode SELL-ONLY: hanya menjual CC → USDCx dengan 130 CC fixed per tx.
    Tidak ada swap balik. Loop terus sampai balance CC habis / paused / stopped.
    """
    name = acc["name"]
    pair = "CC/USDCx"
    cfg  = state["config"]

    log.info(f"[{name}] [SELL-ONLY] Starting — sell 130 CC → USDCx per tx")
    _set_account(name, status="starting", phase="[SELL-ONLY] initializing",
                 last_msg="SELL-ONLY mode: 130 CC → USDCx per tx")

    if not SDK_AVAILABLE:
        # Demo loop sell-only
        bal_a = 500.0
        bal_b = 0.0
        while True:
            a = _get_account(name)
            if not a:
                break
            if a.get("is_paused"):
                _set_account(name, status="paused", phase="paused by user")
                await asyncio.sleep(3)
                continue
            if bal_a < 130:
                _set_account(name, status="stopped", phase="balance CC habis",
                             last_msg=f"CC balance tidak cukup ({bal_a:.2f} CC < 130 CC)")
                break
            bal_a -= 130
            bal_b += round(130 * random.uniform(1.40, 1.55), 4)
            write_log({"acc": name, "pair": pair, "action": "Amulet→USDCx",
                       "amount": "130", "out": str(bal_b), "fee": "0.05", "status": "SUCCESS"})
            _set_account(name, swap_count=a.get("swap_count", 0) + 1,
                         status="running", phase="CC → USDCx",
                         last_msg=f"Sold 130 CC | bal CC={bal_a:.2f}",
                         balA=f"{bal_a:.2f} Amulet", balB=f"{bal_b:.4f} USDCx",
                         network_fee=f"{round(random.uniform(0.03, 0.08), 3)} CC")
            delay_loop = int(cfg.get("delay_per_loop", 600))
            await asyncio.sleep(delay_loop)
        return

    try:
        operator = OperatorKeySigner.from_hex(acc["operator_key"])
        intent   = IntentTradingKeySigner.from_hex(acc["trading_key"])
        base_url = cfg.get("base_url", "https://api.cantex.io")

        async with CantexSDK(operator, intent, base_url=base_url,
                             api_key_path=f"secrets/api_key_{name}.txt") as sdk:

            _set_account(name, phase="authenticating", last_msg="Authenticating...")
            await sdk.authenticate()

            admin_info = await sdk.get_account_admin()
            if not admin_info.has_intent_account:
                _set_account(name, phase="setup", last_msg="Creating intent account...")
                await sdk.create_intent_trading_account()

            _set_account(name, phase="resolving pair", last_msg="Looking up pool CC/USDCx...")
            pools_info = await sdk.get_pool_info()
            try:
                token_a, token_b = _resolve_pair(pools_info, pair)
                log.info(f"[{name}] [SELL-ONLY] Pair resolved: {token_a.id} → {token_b.id}")
                _set_account(name, status="running", phase="idle",
                             last_msg=f"SELL-ONLY: {token_a.id} → {token_b.id} | 130 CC/tx")
            except ValueError as e:
                _set_account(name, status="error", phase="error", last_msg=str(e))
                log.error(f"[{name}] {e}")
                return

            while True:
                a = _get_account(name)
                if not a:
                    break
                if a.get("is_paused"):
                    _set_account(name, status="paused", phase="paused by user")
                    await asyncio.sleep(5)
                    continue

                max_fee         = Decimal(str(cfg.get("max_network_fee", 0.5)))
                max_slippage    = Decimal(str(cfg.get("max_slippage", 0.05)))
                delay_loop      = int(cfg.get("delay_per_loop", 600))
                fee_check_iv    = int(cfg.get("fee_check_interval", 15))
                confirm_timeout = float(cfg.get("confirm_timeout", 120.0))
                sell_amount     = SELL_ONLY_AMOUNT  # 130 CC fixed

                # Cek balance
                try:
                    info  = await sdk.get_account_info()
                    bal_a = info.get_balance(token_a)
                    bal_b = info.get_balance(token_b)
                    _set_account(name,
                        balA=_fmt(bal_a, token_a.id),
                        balB=_fmt(bal_b, token_b.id),
                        status="running",
                    )
                except Exception as e:
                    _set_account(name, last_msg=f"Balance error: {e}")
                    await asyncio.sleep(10)
                    continue

                if bal_a < sell_amount:
                    _set_account(name,
                        status="stopped", phase="CC balance tidak cukup",
                        last_msg=f"CC ({_fmt(bal_a)}) < 130 — SELL-ONLY stopped")
                    log.warning(f"[{name}] [SELL-ONLY] CC balance {bal_a} < 130. Stopping.")
                    break

                # Fee & slippage check
                ok, net_fee = await _check_fee_and_slippage(
                    sdk, token_a, token_b, sell_amount,
                    max_fee, max_slippage, name, fee_check_iv)
                if not ok:
                    await asyncio.sleep(delay_loop)
                    continue

                _set_account(name,
                    phase=f"[SELL-ONLY] {token_a.id} → {token_b.id}",
                    last_msg=f"Selling {_fmt(sell_amount)} {token_a.id}...")
                try:
                    event = await sdk.swap_and_confirm(
                        sell_amount=sell_amount,
                        sell_instrument=token_a,
                        buy_instrument=token_b,
                        timeout=confirm_timeout,
                    )
                    out_amt        = event.output_amount
                    net_fee_actual = event.admin_fee_amount + event.liquidity_fee_amount
                    _set_account(name,
                        last_msg=f"[SELL-ONLY] Got {_fmt(out_amt, token_b.id)} | fee={_fmt(net_fee_actual)}",
                        swap_count=((_get_account(name) or {}).get("swap_count", 0) + 1),
                    )
                    write_log({
                        "acc": name, "pair": pair, "mode": "sell_only",
                        "action": f"{token_a.id}→{token_b.id}",
                        "amount": str(sell_amount), "out": str(out_amt),
                        "fee": str(net_fee_actual), "price": str(event.price),
                        "status": "SUCCESS",
                    })
                except CantexTimeoutError:
                    _set_account(name, last_msg=f"[SELL-ONLY] Swap timeout {confirm_timeout}s")
                    write_log({"acc": name, "pair": pair, "mode": "sell_only",
                               "action": f"{token_a.id}→{token_b.id}",
                               "amount": str(sell_amount), "status": "TIMEOUT"})
                    await asyncio.sleep(delay_loop)
                    continue
                except Exception as e:
                    _set_account(name, last_msg=f"[SELL-ONLY] Swap failed: {e}")
                    write_log({"acc": name, "pair": pair, "mode": "sell_only",
                               "action": f"{token_a.id}→{token_b.id}",
                               "amount": str(sell_amount), "status": "FAILED",
                               "error": str(e)})
                    await asyncio.sleep(delay_loop)
                    continue

                _set_account(name,
                    phase=f"[SELL-ONLY] cooldown {delay_loop}s",
                    last_msg=f"Next sell in {delay_loop}s")
                await asyncio.sleep(delay_loop)

    except asyncio.CancelledError:
        _set_account(name, status="stopped", phase="—", last_msg="Bot stopped")
    except CantexAuthError as e:
        _set_account(name, status="error", last_msg=f"Auth error: {str(e)[:80]}")
        log.error(f"[{name}] Auth error: {e}")
    except Exception as e:
        log.error(f"[{name}] [SELL-ONLY] Fatal error: {e}", exc_info=True)
        _set_account(name, status="error", phase="error", last_msg=str(e)[:80])


async def run_account_bot(acc: dict):
    name = acc["name"]
    pair = acc.get("pair", "CC/USDCx")
    cfg  = state["config"]

    log.info(f"[{name}] Bot starting (pair={pair}, mainnet)...")
    _set_account(name, status="starting", phase="initializing",
                 last_msg=f"Bot starting (pair={pair})...")

    if not SDK_AVAILABLE:
        await _demo_loop(name)
        return

    try:
        operator = OperatorKeySigner.from_hex(acc["operator_key"])
        intent   = IntentTradingKeySigner.from_hex(acc["trading_key"])
        base_url = cfg.get("base_url", "https://api.cantex.io")

        async with CantexSDK(operator, intent, base_url=base_url,
                             api_key_path=f"secrets/api_key_{name}.txt") as sdk:

            _set_account(name, phase="authenticating", last_msg="Authenticating...")
            await sdk.authenticate()

            admin_info = await sdk.get_account_admin()
            if not admin_info.has_intent_account:
                _set_account(name, phase="setup", last_msg="Creating intent account...")
                await sdk.create_intent_trading_account()

            _set_account(name, phase="resolving pair", last_msg=f"Looking up pool for {pair}...")
            pools_info = await sdk.get_pool_info()
            try:
                token_a, token_b = _resolve_pair(pools_info, pair)
                log.info(f"[{name}] Pair resolved: {token_a.id}({token_a.admin[:20]}...) "
                         f"<-> {token_b.id}({token_b.admin[:20]}...)")
                _set_account(name, status="running", phase="idle",
                             last_msg=f"Pool found: {token_a.id}/{token_b.id}")
            except ValueError as e:
                _set_account(name, status="error", phase="error", last_msg=str(e))
                log.error(f"[{name}] {e}")
                return


            while True:
                a = _get_account(name)
                if not a:
                    break
                if a.get("is_paused"):
                    _set_account(name, status="paused", phase="paused by user")
                    await asyncio.sleep(5)
                    continue

                swap_amount_a       = Decimal(str(cfg.get("swap_amount_a", 1.0)))
                max_fee             = Decimal(str(cfg.get("max_network_fee", 0.5)))
                max_slippage        = Decimal(str(cfg.get("max_slippage", 0.05)))
                delay_a_to_b        = int(cfg.get("delay_a_to_b", 30))
                delay_loop          = int(cfg.get("delay_per_loop", 60))
                use_full_b          = bool(cfg.get("use_full_b", True))
                swap_amount_b_fixed = Decimal(str(cfg.get("swap_amount_b", 10.0)))
                fee_check_iv        = int(cfg.get("fee_check_interval", 15))
                confirm_timeout     = float(cfg.get("confirm_timeout", 120.0))

                try:
                    info  = await sdk.get_account_info()
                    bal_a = info.get_balance(token_a)
                    bal_b = info.get_balance(token_b)
                    _set_account(name,
                        balA=_fmt(bal_a, token_a.id),
                        balB=_fmt(bal_b, token_b.id),
                        status="running",
                    )
                except Exception as e:
                    _set_account(name, last_msg=f"Balance error: {e}")
                    await asyncio.sleep(10)
                    continue

                if bal_a >= swap_amount_a:
                    ok, net_fee = await _check_fee_and_slippage(
                        sdk, token_a, token_b, swap_amount_a,
                        max_fee, max_slippage, name, fee_check_iv)
                    if not ok:
                        await asyncio.sleep(delay_loop)
                        continue

                    _set_account(name,
                        phase=f"{token_a.id} → {token_b.id}",
                        last_msg=f"Swapping {_fmt(swap_amount_a)} {token_a.id}...")
                    try:
                        event = await sdk.swap_and_confirm(
                            sell_amount=swap_amount_a,
                            sell_instrument=token_a,
                            buy_instrument=token_b,
                            timeout=confirm_timeout,
                        )
                        out_amt        = event.output_amount
                        net_fee_actual = event.admin_fee_amount + event.liquidity_fee_amount
                        _set_account(name,
                            last_msg=f"Got {_fmt(out_amt, token_b.id)} | fee={_fmt(net_fee_actual)}",
                        )
                        write_log({
                            "acc": name, "pair": pair,
                            "action": f"{token_a.id}→{token_b.id}",
                            "amount": str(swap_amount_a), "out": str(out_amt),
                            "fee": str(net_fee_actual), "price": str(event.price),
                            "status": "SUCCESS",
                        })
                        _set_account(name, swap_count=((_get_account(name) or {}).get("swap_count", 0) + 1))
                    except CantexTimeoutError:
                        _set_account(name, last_msg=f"Swap A→B timed out after {confirm_timeout}s")
                        write_log({"acc": name, "pair": pair,
                                   "action": f"{token_a.id}→{token_b.id}",
                                   "amount": str(swap_amount_a), "status": "TIMEOUT"})
                        await asyncio.sleep(delay_loop)
                        continue
                    except Exception as e:
                        _set_account(name, last_msg=f"Swap A→B failed: {e}")
                        write_log({"acc": name, "pair": pair,
                                   "action": f"{token_a.id}→{token_b.id}",
                                   "amount": str(swap_amount_a), "status": "FAILED",
                                   "error": str(e)})
                        await asyncio.sleep(delay_loop)
                        continue

                    _set_account(name,
                        phase=f"waiting {delay_a_to_b}s before swap back",
                        last_msg=f"Waiting {delay_a_to_b}s...")
                    await asyncio.sleep(delay_a_to_b)
                else:
                    _set_account(name, phase="low balance A",
                        last_msg=f"Insufficient {token_a.id} ({_fmt(bal_a)}). Checking {token_b.id}...")

                try:
                    info      = await sdk.get_account_info()
                    bal_a_now = info.get_balance(token_a)
                    bal_b_now = info.get_balance(token_b)
                    _set_account(name,
                        balA=_fmt(bal_a_now, token_a.id),
                        balB=_fmt(bal_b_now, token_b.id)
                    )
                except Exception:
                    bal_b_now = Decimal("0")

                amount_b = (
                    bal_b_now.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
                    if use_full_b else swap_amount_b_fixed
                )

                if amount_b <= Decimal("0"):
                    _set_account(name, last_msg=f"No {token_b.id} to swap back")
                    await asyncio.sleep(delay_loop)
                    continue

                ok, _ = await _check_fee_and_slippage(
                    sdk, token_b, token_a, amount_b,
                    max_fee, max_slippage, name, fee_check_iv)
                if not ok:
                    continue

                _set_account(name,
                    phase=f"{token_b.id} → {token_a.id}",
                    last_msg=f"Swapping {_fmt(amount_b)} {token_b.id}...")
                try:
                    event = await sdk.swap_and_confirm(
                        sell_amount=amount_b,
                        sell_instrument=token_b,
                        buy_instrument=token_a,
                        timeout=confirm_timeout,
                    )
                    out_amt = event.output_amount
                    _set_account(name,
                        last_msg=f"Got {_fmt(out_amt, token_a.id)} back | price={event.price}")
                    write_log({
                        "acc": name, "pair": pair,
                        "action": f"{token_b.id}→{token_a.id}",
                        "amount": str(amount_b), "out": str(out_amt),
                        "price": str(event.price), "status": "SUCCESS",
                    })
                    _set_account(name, swap_count=((_get_account(name) or {}).get("swap_count", 0) + 1))
                except CantexTimeoutError:
                    _set_account(name, last_msg=f"Swap B→A timed out after {confirm_timeout}s")
                    write_log({"acc": name, "pair": pair,
                               "action": f"{token_b.id}→{token_a.id}",
                               "amount": str(amount_b), "status": "TIMEOUT"})
                except Exception as e:
                    _set_account(name, last_msg=f"Swap B→A failed: {e}")
                    write_log({"acc": name, "pair": pair,
                               "action": f"{token_b.id}→{token_a.id}",
                               "amount": str(amount_b), "status": "FAILED",
                               "error": str(e)})

                _set_account(name,
                    phase=f"loop cooldown {delay_loop}s",
                    last_msg=f"Next round in {delay_loop}s")
                await asyncio.sleep(delay_loop)

    except asyncio.CancelledError:
        _set_account(name, status="stopped", phase="—", last_msg="Bot stopped")
    except CantexAuthError as e:
        _set_account(name, status="error", last_msg=f"Auth error: {str(e)[:80]}")
        log.error(f"[{name}] Auth error: {e}")
    except Exception as e:
        log.error(f"[{name}] Fatal error: {e}", exc_info=True)
        _set_account(name, status="error", phase="error", last_msg=str(e)[:80])


async def _check_fee_and_slippage(
    sdk, sell: InstrumentId, buy: InstrumentId,
    amount: Decimal, max_fee: Decimal, max_slippage: Decimal,
    name: str, check_iv: int,
):
    was_waiting = False

    while True:
        a = _get_account(name)
        if not a:
            return False, Decimal("0")
        if a.get("is_paused"):
            return False, Decimal("0")

        jitter = random.uniform(0, 8)
        await asyncio.sleep(jitter)

        try:
            quote     = await sdk.get_swap_quote(
                sell_amount=amount,
                sell_instrument=sell,
                buy_instrument=buy,
            )
            net_fee   = quote.fees.network_fee.amount
            slippage  = quote.prices.slippage
            fee_token = quote.fees.network_fee.instrument.id

            _set_account(name, network_fee=f"{_fmt(net_fee)} {fee_token}")

            if net_fee > max_fee:
                was_waiting = True
                _set_account(name,
                    status="waiting_fee",
                    phase=f"fee={_fmt(net_fee)} > max={_fmt(max_fee)}",
                    last_msg=f"Network fee {_fmt(net_fee)} {fee_token} too high. "
                             f"Waiting... (check every {check_iv}s)")
                log.info(f"[{name}] Fee {net_fee} > max {max_fee}, waiting {check_iv}s...")
                await asyncio.sleep(check_iv)
                continue

            if slippage > max_slippage:
                was_waiting = True
                _set_account(name,
                    status="waiting_fee",
                    phase=f"slip={float(slippage)*100:.2f}% > max={float(max_slippage)*100:.2f}%",
                    last_msg=f"Slippage {float(slippage)*100:.2f}% too high. "
                             f"Waiting... (check every {check_iv}s)")
                await asyncio.sleep(check_iv)
                continue

            if was_waiting:
                log.info(f"[{name}] Fee OK ({net_fee}), proceeding after {jitter:.1f}s stagger.")

            _set_account(name, status="running")
            return True, net_fee

        except Exception as e:
            _set_account(name, last_msg=f"Quote error: {e}")
            log.warning(f"[{name}] Quote error (will retry in {check_iv}s): {e}")
            await asyncio.sleep(check_iv)
            continue


async def _demo_loop(name: str):
    acc  = _get_account(name)
    pair = (acc or {}).get("pair", "CC/USDCx")
    id_a, id_b = SUPPORTED_PAIRS.get(pair, ("CC", "USDCx"))

    bal_a = 150.0
    bal_b = 100.0

    phases = [
        ("running",     f"{id_a} → {id_b}",    f"Swapping 10.0 {id_a}..."),
        ("running",     "waiting 30s",           f"Got 14.5 {id_b}"),
        ("running",     f"{id_b} → {id_a}",     f"Swapping 14.5 {id_b}..."),
        ("running",     "loop cooldown",          "Next round in 600s"),
        ("waiting_fee", f"fee=0.8 > max=0.15",   "Network fee high, checking..."),
        ("running",     f"{id_a} → {id_b}",      f"Swapping 10.0 {id_a}..."),
    ]
    i = 0
    while True:
        a = _get_account(name)
        if not a:
            break
        if a.get("is_paused"):
            _set_account(name, status="paused", phase="paused by user")
            await asyncio.sleep(3)
            continue

        status, phase, msg = phases[i % len(phases)]

        is_success_a_b = (phase == "waiting 30s")
        is_success_b_a = (phase == "loop cooldown")

        if is_success_a_b:
            bal_a -= 10.0
            bal_b += 14.5
            write_log({"acc": name, "pair": pair, "action": f"{id_a}→{id_b}", "amount": "10.0", "out": "14.5", "fee": "0.1", "status": "SUCCESS"})
            _set_account(name, swap_count=a.get("swap_count", 0) + 1)

        if is_success_b_a:
            bal_a += 9.8
            bal_b -= 14.5
            write_log({"acc": name, "pair": pair, "action": f"{id_b}→{id_a}", "amount": "14.5", "out": "9.8", "fee": "0.1", "status": "SUCCESS"})
            _set_account(name, swap_count=a.get("swap_count", 0) + 1)

        _set_account(name, status=status, phase=phase, last_msg=msg,
                     balA=f"{round(bal_a, 2)} {id_a}", balB=f"{round(bal_b, 2)} {id_b}",
                     network_fee=f"{round(random.uniform(0.1, 0.9), 3)} CC")
        i += 1
        await asyncio.sleep(4)


# ── Lifecycle ────────────────────────────────────────────────────
def _get_or_create_loop():
    loop = state.get("bot_loop")
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        state["bot_loop"] = loop
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()
    return loop

def start_account_task(acc: dict):
    name = acc["name"]
    loop = _get_or_create_loop()
    old  = state["bot_tasks"].get(name)
    if old:
        loop.call_soon_threadsafe(old.cancel)
    # Pilih coroutine berdasarkan mode global
    if BOT_MODE == "sell_only":
        coro = run_account_bot_sell_only(acc)
        mode_label = "SELL-ONLY"
    else:
        coro = run_account_bot(acc)
        mode_label = "NORMAL"
    task = asyncio.run_coroutine_threadsafe(coro, loop)
    state["bot_tasks"][name] = task
    log.info(f"[{name}] Task started — {BOT_INFO['name']} {BOT_INFO['product']} [{mode_label} MODE]")

def stop_account_task(name: str):
    task = state["bot_tasks"].pop(name, None)
    if task:
        task.cancel()
    _set_account(name, status="stopped", phase="—", last_msg="Bot stopped")

def start_all():
    for acc in state["accounts"]:
        start_account_task(acc)

def stop_all():
    for name in list(state["bot_tasks"].keys()):
        stop_account_task(name)


# ── SSE Broadcaster ──────────────────────────────────────────────
def _sse_broadcaster():
    while True:
        try:
            broadcast_state()
        except Exception as e:
            log.warning(f"SSE broadcast error: {e}")
        time.sleep(2)


# ── Routes ───────────────────────────────────────────────────────
@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json(force=True) or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    if not _auth['username'] or not _auth['password_hash']:
        return jsonify({'ok': False, 'message': 'Auth not configured. Create auth.json first.'}), 503

    if username == _auth['username'] and _hash_password(password) == _auth['password_hash']:
        sid = secrets.token_hex(32)
        _sessions[sid] = time.time()
        log.info(f'[Auth] Admin logged in')
        resp = jsonify({'ok': True, 'message': 'Login successful'})
        resp.set_cookie(
            'cantex_sid', sid,
            httponly=True, samesite='Strict',
            max_age=_auth['session_ttl'], path='/'
        )
        return resp
    return jsonify({'ok': False, 'message': 'Invalid username or password'}), 401

@app.route('/api/logout', methods=['POST'])
def api_logout():
    sid = request.cookies.get('cantex_sid')
    if sid:
        _sessions.pop(sid, None)
    log.info('[Auth] Admin logged out')
    resp = jsonify({'ok': True, 'message': 'Logged out'})
    resp.set_cookie('cantex_sid', '', httponly=True, samesite='Strict', max_age=0, path='/')
    return resp

@app.route('/api/auth-status', methods=['GET'])
def api_auth_status():
    return jsonify({'isLoggedIn': is_authenticated()})

@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'home.html')

@app.route('/api/stream')
def sse_stream():
    import queue
    q = queue.Queue(maxsize=50)
    state["sse_clients"].append(q)

    def generate():
        try:
            yield "data: {}\n\n"
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield msg
                except Exception:
                    yield ": ping\n\n"
        except GeneratorExit:
            pass
        finally:
            try:
                state["sse_clients"].remove(q)
            except Exception:
                pass

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route('/api/settings', methods=['GET'])
def get_settings():
    return jsonify(state["config"])

@app.route('/api/settings', methods=['POST'])
def post_settings():
    if not is_authenticated():
        return require_auth()
    data = request.get_json(force=True) or {}
    updated = {}
    for k, v in data.items():
        if k in state["config"] or k in DEFAULT_CONFIG:
            state["config"][k] = v
            updated[k] = v
    save_config()
    return jsonify({"ok": True, "updated": updated})

@app.route('/api/pairs', methods=['GET'])
def get_pairs():
    return jsonify({"pairs": list(SUPPORTED_PAIRS.keys())})

@app.route('/api/accounts', methods=['GET'])
def get_accounts():
    with _lock:
        result = []
        for a in state["accounts"]:
            result.append({
                "name":        a.get("name"),
                "operatorKey": (a.get("operator_key") or "")[:12] + "...",
                "pair":        a.get("pair", "CC/USDCx"),
                "status":      a.get("status", "stopped"),
                "phase":       a.get("phase", "—"),
                "balA":        a.get("balA", "—"),
                "balB":        a.get("balB", "—"),
                "swapCount":   a.get("swap_count", 0),
                "lastMsg":     a.get("last_msg", ""),
                "isPaused":    a.get("is_paused", False),
                "networkFee":  a.get("network_fee", "—"),
            })
    return jsonify(result)

@app.route('/api/accounts/add', methods=['POST'])
def add_accounts():
    if not is_authenticated():
        return require_auth()
    data = request.get_json(force=True) or {}
    required = ["name", "operator_key", "trading_key"]
    for field in required:
        if not data.get(field):
            return jsonify({"ok": False, "error": f"Missing field: {field}"}), 400

    pair = data.get("pair", "CC/USDCx")
    if pair not in SUPPORTED_PAIRS:
        return jsonify({
            "ok": False,
            "error": f"Invalid pair '{pair}'. Supported: {list(SUPPORTED_PAIRS.keys())}"
        }), 400

    name = data["name"].strip()
    with _lock:
        for a in state["accounts"]:
            if a.get("name") == name:
                return jsonify({"ok": False, "error": f"Account '{name}' already exists"}), 400

    acc = {
        "name":          name,
        "operator_key":  data["operator_key"].strip(),
        "trading_key":   data["trading_key"].strip(),
        "pair":          pair,
        "status":        "stopped",
        "phase":         "—",
        "balA":          "—",
        "balB":          "—",
        "swap_count":    0,
        "last_msg":      "Account added",
        "is_paused":     False,
        "network_fee":   "—",
    }
    with _lock:
        state["accounts"].append(acc)
    save_accounts()

    if state["bot_tasks"] or data.get("autostart", True):
        start_account_task(acc)

    return jsonify({"ok": True, "added": 1, "name": name, "pair": pair})

@app.route('/api/accounts/remove', methods=['POST'])
def remove_account():
    if not is_authenticated():
        return require_auth()
    data = request.get_json(force=True) or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    stop_account_task(name)
    with _lock:
        state["accounts"] = [a for a in state["accounts"] if a.get("name") != name]
    save_accounts()
    return jsonify({"ok": True})

@app.route('/api/bot/status', methods=['GET'])
def bot_status():
    is_running    = bool(state["bot_tasks"])
    accounts_data = [
        {"name": a.get("name"), "isPaused": a.get("is_paused", False),
         "pair": a.get("pair", "CC/USDCx")}
        for a in state["accounts"]
    ]
    return jsonify({"isRunning": is_running, "accounts": accounts_data})

@app.route('/api/bot/start', methods=['POST'])
def bot_start():
    if not is_authenticated():
        return require_auth()
    start_all()
    return jsonify({"ok": True})

@app.route('/api/bot/stop', methods=['POST'])
def bot_stop():
    if not is_authenticated():
        return require_auth()
    stop_all()
    return jsonify({"ok": True})

@app.route('/api/bot/pause', methods=['POST'])
def bot_pause():
    if not is_authenticated():
        return require_auth()
    idx = request.args.get("index")
    if idx is not None:
        try:
            acc = state["accounts"][int(idx)]
            _set_account(acc["name"], is_paused=True)
        except (IndexError, ValueError):
            return jsonify({"ok": False, "error": "invalid index"}), 400
    else:
        for a in state["accounts"]:
            _set_account(a["name"], is_paused=True)
    return jsonify({"ok": True})

@app.route('/api/bot/resume', methods=['POST'])
def bot_resume():
    if not is_authenticated():
        return require_auth()
    idx = request.args.get("index")
    if idx is not None:
        try:
            acc = state["accounts"][int(idx)]
            _set_account(acc["name"], is_paused=False, status="running")
        except (IndexError, ValueError):
            return jsonify({"ok": False, "error": "invalid index"}), 400
    else:
        for a in state["accounts"]:
            _set_account(a["name"], is_paused=False)
    return jsonify({"ok": True})

@app.route('/api/bot/restart', methods=['POST'])
def bot_restart():
    if not is_authenticated():
        return require_auth()
    stop_all()
    time.sleep(1)
    start_all()
    return jsonify({"ok": True})

@app.route('/api/swaplogs', methods=['GET'])
def swap_logs():
    page  = int(request.args.get("page",  1))
    limit = int(request.args.get("limit", 25))
    items, total = read_logs(page, limit)
    return jsonify({"items": items, "total": total, "page": page})

@app.route('/api/cc-history', methods=['GET'])
def cc_history():
    days = int(request.args.get("days", 30))
    days = max(1, min(days, 365))
    return jsonify({"history": cc_daily_history(days)})

@app.route('/api/info', methods=['GET'])
def bot_info():
    return jsonify(BOT_INFO)

@app.route('/api/mode', methods=['GET'])
def bot_mode():
    mode_label = "SELL-ONLY (130 CC/tx)" if BOT_MODE == "sell_only" else "NORMAL (CC ↔ USDCx)"
    return jsonify({
        "mode":        BOT_MODE,
        "mode_label":  mode_label,
        "sell_amount": str(SELL_ONLY_AMOUNT) if BOT_MODE == "sell_only" else None,
    })

@app.route('/api/stats', methods=['GET'])
def stats():
    with _lock:
        total_swaps = sum(a.get("swap_count", 0) for a in state["accounts"])
        active      = sum(1 for a in state["accounts"] if a.get("status") in ("running", "waiting_fee"))
    uptime_s = int(time.time() - state["start_time"])
    return jsonify({
        "bot":            BOT_INFO,
        "totalAccounts":  len(state["accounts"]),
        "activeAccounts": active,
        "totalSwaps":     total_swaps,
        "totalSuccess":   total_swaps,
        "totalFailed":    0,
        "uptimeSeconds":  uptime_s,
    })

@app.route('/api/uptime', methods=['GET'])
def uptime():
    s = int(time.time() - state["start_time"])
    h, rem = divmod(s, 3600)
    m, sec  = divmod(rem, 60)
    return jsonify({"seconds": s, "formatted": f"{h:02d}:{m:02d}:{sec:02d}"})

@app.route('/api/pools', methods=['GET'])
def get_pools():
    return jsonify({
        "supported_pairs": list(SUPPORTED_PAIRS.keys()),
        "note": "Live pool data requires an authenticated account. "
                "Pool instrument IDs are resolved automatically at bot startup.",
    })


# ── Startup ──────────────────────────────────────────────────────
if __name__ == '__main__':
    os.makedirs(os.path.join(BASE_DIR, 'secrets'), exist_ok=True)
    load_config()
    load_accounts()
    load_auth()

    # ── First-time auth setup ─────────────────────────────────────
    if not _auth['username'] or not _auth['password_hash']:
        print()
        print("=" * 52)
        print("  SOJUU CANTEX BOT — First-Time Setup")
        print("  Set your admin credentials for the web dashboard.")
        print("=" * 52)
        print()

        username = ""
        while not username:
            username = input("  Username : ").strip()
            if not username:
                print("  ⚠  Username cannot be empty.")

        import getpass
        while True:
            password = getpass.getpass("  Password : ")
            if len(password) < 6:
                print("  ⚠  Password must be at least 6 characters. Try again.\n")
                continue
            confirm = getpass.getpass("  Confirm  : ")
            if password != confirm:
                print("  ⚠  Passwords do not match. Try again.\n")
            else:
                break

        pw_hash = _hash_password(password)
        save_auth(username, pw_hash)

        print()
        print(f"  ✓ Credentials saved to auth.json")
        print(f"  Username : {username}")
        print(f"  Password : {'*' * len(password)}")
        print()
        print("=" * 52)
        print()

    # ── Pilih Mode Bot ────────────────────────────────────────────
    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║          SOJUU BOT — CANTEX V2                       ║")
    print("║          Pilih Mode Operasi Bot                      ║")
    print("╠══════════════════════════════════════════════════════╣")
    print("║                                                      ║")
    print("║  [1] Mode Normal   — Swap CC ↔ USDCx (2 arah)       ║")
    print("║                      Amount dari config              ║")
    print("║                                                      ║")
    print("║  [2] Mode Sell-Only— Jual CC → USDCx SAJA           ║")
    print("║                      Fixed 130 CC per transaksi      ║")
    print("║                                                      ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()

    mode_choice = ""
    while mode_choice not in ("1", "2"):
        mode_choice = input("  Pilih mode [1/2] : ").strip()
        if mode_choice not in ("1", "2"):
            print("  ⚠  Masukkan 1 atau 2.")

    if mode_choice == "2":
        BOT_MODE = "sell_only"
        print()
        print("  ✓ Mode dipilih : SELL-ONLY (130 CC → USDCx per tx)")
        print(f"  ✓ Jumlah akun  : {len(state['accounts'])} akun")
        print()
    else:
        BOT_MODE = "normal"
        print()
        print("  ✓ Mode dipilih : NORMAL (CC ↔ USDCx, 2 arah)")
        print(f"  ✓ Jumlah akun  : {len(state['accounts'])} akun")
        print()

    # Teruskan nilai ke fungsi-fungsi yang membutuhkan
    import builtins
    builtins.BOT_MODE = BOT_MODE  # expose ke module scope
    # Update module-level variable
    import sys
    sys.modules[__name__].BOT_MODE = BOT_MODE

    if state["accounts"]:
        log.info(f"Auto-starting {len(state['accounts'])} account(s) in [{BOT_MODE.upper()}] mode...")
        start_all()

    t = threading.Thread(target=_sse_broadcaster, daemon=True)
    t.start()

    mode_str = "SELL-ONLY (130 CC/tx)" if BOT_MODE == "sell_only" else "NORMAL (CC ↔ USDCx)"
    log.info("=" * 54)
    log.info(f"  {BOT_INFO['name']} — {BOT_INFO['product']}")
    log.info(f"  {BOT_INFO['credit']}")
    log.info(f"  Network : {BOT_INFO['network']}")
    log.info(f"  Mode    : {mode_str}")
    log.info(f"  Base URL: {state['config'].get('base_url')}")
    log.info(f"  Pairs   : {list(SUPPORTED_PAIRS.keys())}")
    log.info(f"  Accounts: {len(state['accounts'])} loaded")
    log.info("  Frontend: http://localhost:5001")
    log.info("=" * 54)

    app.run(host='0.0.0.0', port=5001, debug=False, threaded=True)
