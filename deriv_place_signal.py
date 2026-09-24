#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
  Ejecuta la orden en DERIV (API NUEVA 2026: PAT + OTP → WebSocket DEMO)
  según la señal de senal_diaria.py. Se invoca como SUBPROCESO.

  FLUJO (solo cuenta DEMO — nunca la real):
    1) GET  /trading/v1/options/accounts          → elige account_type == "demo"
    2) POST /trading/v1/options/accounts/{id}/otp → URL del WS de la demo
    3) connect al WS (sin authorize: el OTP ya autentica)
    4) proposal (MULTUP/MULTDOWN con SL/TP) → buy

  Usa contratos MULTIPLIER con Stop Loss y Take Profit NATIVOS en USD
  (limit_order) — el servidor cierra la posición al tocar SL/TP.

  Variables de entorno:
    DERIV_API_TOKEN   PAT de developers.deriv.com (scope: Trade)  [obligatorio]
    DERIV_APP_ID      App ID de la app (obligatorio para PAT)
    DERIV_SIDE        BUY | SELL
    DERIV_SYMBOL      frxAUDNZD | cryBTCUSD | frxXAUUSD (o alias AUDNZD/BTC/XAUUSD)
    SIGNAL_ENTRY      precio de entrada de la señal
    SIGNAL_SL         stop loss (precio)
    SIGNAL_TP         take profit (precio)
    STAKE_USD         apuesta base en USD (default 10)
    MULTIPLIER        multiplicador (default 100)
    DERIV_DRY_RUN     1 = solo imprime la orden sin conectar

  Conversión precio -> USD (multipliers):
    P&L_USD = stake × multiplicador × (Δprecio / entrada)
    stop_loss  = stake × mult × |entrada − SL| / entrada
    take_profit = 3 × stop_loss   (R:R 1:3 exacto)
"""
import asyncio
import json
import os
import time
import urllib.error
import urllib.request

try:
    import websockets
except ImportError:
    print("❌ Falta la librería 'websockets'. Instala: pip install websockets")
    raise SystemExit(2)


def e(key, default=None):
    return os.environ.get(key, default)


API_TOKEN = (e("DERIV_API_TOKEN", "") or "").strip()
APP_ID    = (e("DERIV_APP_ID", "") or "").strip()
DRY_RUN   = (e("DERIV_DRY_RUN", "") or "").lower() in ("1", "true", "yes")

SIDE   = (e("DERIV_SIDE", "BUY") or "BUY").upper()
SYMBOL = (e("DERIV_SYMBOL", "AUDNZD") or "AUDNZD").upper()

ENTRY = float(e("SIGNAL_ENTRY", "0") or 0)
SL    = float(e("SIGNAL_SL", "0") or 0)
TP    = float(e("SIGNAL_TP", "0") or 0)

STAKE      = float(e("STAKE_USD", "10") or 10)
MULTIPLIER = float(e("MULTIPLIER", "100") or 100)

ALIAS = {
    "AUDNZD": "frxAUDNZD", "FRXAUDNZD": "frxAUDNZD",
    "BTC": "cryBTCUSD", "BTCUSD": "cryBTCUSD", "CRYBTCUSD": "cryBTCUSD",
    "XAU": "frxXAUUSD", "XAUUSD": "frxXAUUSD", "GOLD": "frxXAUUSD",
    "FRXXAUUSD": "frxXAUUSD",
}
DERIV_SYMBOL = ALIAS.get(SYMBOL, SYMBOL)

CONTRACT = {"BUY": "MULTUP", "SELL": "MULTDOWN"}
REST = "https://api.derivws.com"
MIN_USD = 0.35


def fail(msg):
    print(f"❌ {msg}")
    raise SystemExit(1)


def calc_usd():
    """Convierte los niveles de precio de la señal a montos USD de SL/TP."""
    if SIDE not in CONTRACT:
        fail(f"side inválido: {SIDE} (usa BUY o SELL)")
    if ENTRY <= 0 or SL <= 0 or TP <= 0:
        fail(f"Faltan niveles: ENTRY={ENTRY} SL={SL} TP={TP}")

    if SIDE == "BUY":
        if not (SL < ENTRY < TP):
            fail(f"BUY exige SL < entrada < TP (recibí {SL} < {ENTRY} < {TP}?)")
    else:
        if not (TP < ENTRY < SL):
            fail(f"SELL exige TP < entrada < SL (recibí {TP} < {ENTRY} < {SL}?)")

    risk_pct = abs(ENTRY - SL) / ENTRY
    rew_pct  = abs(TP - ENTRY) / ENTRY

    # Stop-out: Deriv cierra el multiplier ~al 100% del stake.
    # SL en USD ≤ 90% del stake → mult × risk_pct ≤ 0.9
    mult = MULTIPLIER
    adj_mult = False
    if risk_pct > 0 and mult * risk_pct > 0.9:
        mult = max(1.0, int(0.9 / risk_pct))
        adj_mult = True

    sl_usd = STAKE * mult * risk_pct
    tp_usd = STAKE * mult * rew_pct

    sl_usd, tp_usd = round(sl_usd, 2), round(tp_usd, 2)
    adj = adj_mult
    if sl_usd < MIN_USD:
        sl_usd = MIN_USD; adj = True
    if tp_usd < MIN_USD:
        tp_usd = MIN_USD; adj = True
    if adj:
        tp_usd = round(max(tp_usd, sl_usd * 3.0), 2)

    return sl_usd, tp_usd, adj, mult


def rest(method, path, body=None, timeout=20):
    url = REST + path
    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
        "Accept": "application/json",
        "Deriv-App-ID": APP_ID,
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as ex:
        raw = ex.read().decode(errors="replace")
        try:
            return ex.code, json.loads(raw)
        except Exception:
            return ex.code, {"raw": raw}
    except Exception as ex:
        return 0, {"error": str(ex)}


async def rpc(ws, payload, req_id, timeout=30):
    payload = dict(payload)
    payload["req_id"] = req_id
    await ws.send(json.dumps(payload))
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = await asyncio.wait_for(ws.recv(), timeout=max(1, deadline - time.time()))
        data = json.loads(raw)
        if data.get("req_id") == req_id:
            return data
    fail(f"Timeout esperando respuesta (req_id={req_id})")


def get_demo_otp_ws():
    """GET accounts → demo → POST otp → URL del WebSocket de la demo."""
    print(f"🔐 [DEMO-ONLY] REST con PAT | Deriv-App-ID={APP_ID[:6]}...")
    code, data = rest("GET", "/trading/v1/options/accounts")
    print(f"   GET /accounts -> HTTP {code}")
    if code != 200:
        fail(f"accounts: {data.get('errors') or data}")

    accounts = data.get("data") or []
    if isinstance(accounts, dict):
        accounts = [accounts]

    demo = None
    for a in accounts:
        tipo = a.get("account_type", "?")
        mark = "✅" if tipo == "demo" else "🔒"
        print(f"   {mark} {a.get('account_id')} [{tipo}] "
              f"saldo={a.get('balance')} {a.get('currency', '')}")
        if tipo == "demo" and demo is None:
            demo = a
    if not demo:
        fail("No hay cuenta account_type='demo'. NO opero (solo demo).")

    acc_id = demo.get("account_id")
    print(f"✅ Cuenta DEMO: {acc_id} (saldo {demo.get('balance')})")

    code, data = rest("POST", f"/trading/v1/options/accounts/{acc_id}/otp")
    print(f"   POST /otp -> HTTP {code}")
    if code != 200:
        fail(f"otp: {data.get('errors') or data}")
    ws_url = (data.get("data") or {}).get("url")
    if not ws_url:
        fail(f"otp sin url: {data}")
    print(f"✅ OTP listo -> {ws_url.split('?')[0]}?otp=***")
    return ws_url


async def place_order():
    sl_usd, tp_usd, adj, mult = calc_usd()
    contract = CONTRACT[SIDE]

    print("📋 Orden preparada:")
    print(f"   símbolo     : {DERIV_SYMBOL}")
    print(f"   contrato    : {contract} ({'arriba' if SIDE == 'BUY' else 'abajo'})")
    print(f"   entrada     : {ENTRY}")
    print(f"   SL precio   : {SL}  ->  stop_loss  = {sl_usd} USD")
    print(f"   TP precio   : {TP}  ->  take_profit = {tp_usd} USD  (R:R 1:{tp_usd / sl_usd:.2f})")
    print(f"   stake       : {STAKE} USD × multiplicador {mult:g}"
          + (f" (bajado de {MULTIPLIER:g} para respetar el stop-out)" if mult != MULTIPLIER else ""))
    if adj and mult == MULTIPLIER:
        print("   ⚠️ SL/TP ajustados al mínimo de Deriv (0.35 USD).")

    if DRY_RUN:
        print("✅ DRY_RUN=1: orden NO enviada (solo simulación).")
        return
    if not API_TOKEN:
        fail("Falta DERIV_API_TOKEN (PAT de developers.deriv.com).")
    if not APP_ID:
        fail("Falta DERIV_APP_ID (sin App ID el PAT no sirve).")

    ws_url = get_demo_otp_ws()

    proposal = {
        "proposal": 1,
        "amount": STAKE,
        "basis": "stake",
        "contract_type": contract,
        "currency": "USD",
        "multiplier": mult,
        "symbol": DERIV_SYMBOL,
        "limit_order": {"stop_loss": sl_usd, "take_profit": tp_usd},
    }

    async with websockets.connect(ws_url, open_timeout=20, close_timeout=5) as ws:
        print("✅ ¡WebSocket DEMO conectado! (OTP, sin authorize)")

        r = await rpc(ws, proposal, 2)
        if "error" in r:
            fail(f"proposal: {r['error'].get('code')} — {r['error'].get('message')}")
        prop = r.get("proposal", {})
        pid = prop.get("id")
        payout = prop.get("payout", "?")
        spot = prop.get("spot", "?")
        print(f"✅ Cotización: id={pid} | payout={payout} | spot={spot}")

        r = await rpc(ws, {"buy": pid, "price": STAKE}, 3)
        if "error" in r:
            fail(f"buy: {r['error'].get('code')} — {r['error'].get('message')}")
        b = r.get("buy", {})
        cid = b.get("contract_id")
        paid = b.get("buy_price", STAKE)
        print(f"🚀 ORDEN EJECUTADA (DEMO): {DERIV_SYMBOL} {contract} | stake={paid} USD "
              f"| mult={mult:g} | SL={sl_usd} TP={tp_usd} USD | contract_id={cid}")


def main():
    try:
        asyncio.run(place_order())
    except SystemExit:
        raise
    except Exception as ex:
        fail(f"{type(ex).__name__}: {ex}")


if __name__ == "__main__":
    main()
