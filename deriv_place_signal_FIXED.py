#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
  Ejecuta la orden en DERIV (API NUEVA 2026: PAT + OTP → WebSocket DEMO)
  FIX 2026-09-24: symbol -> underlying_symbol
  FIX 2026-10-20: Damaris GOLD TP muy pegado (3$) -> payout=0, ajusta a min 0.3%
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
DRY_RUN   = (e("DERIV_DRY_RUN", "") or "").lower() in ("1","true","yes")

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
    global TP
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

    # FIX 2026-10-20: Damaris TP muy pegado -> payout=0
    # FIX 2026-10-22: GOLD necesita 0.5% min y 5 USD min para que Deriv no de payout=0
    is_gold = "XAU" in SYMBOL or "GOLD" in SYMBOL or DERIV_SYMBOL == "frxXAUUSD"
    min_rew_pct = 0.005 if is_gold else 0.003
    min_sl_usd = 5.0 if is_gold else MIN_USD
    min_tp_usd = 7.5 if is_gold else MIN_USD

    if rew_pct < min_rew_pct:
        print(f"   ⚠️ TP muy cercano {rew_pct*100:.3f}% < {min_rew_pct*100}% -> ajusto TP a {min_rew_pct*100}% para evitar payout=0")
        if SIDE == "BUY":
            TP = ENTRY * (1 + min_rew_pct)
        else:
            TP = ENTRY * (1 - min_rew_pct)
        rew_pct = abs(TP - ENTRY) / ENTRY

    ALLOWED_MULTS = [100, 200, 300, 500, 800, 1000]
    mult = MULTIPLIER if MULTIPLIER in ALLOWED_MULTS else 100

    adj = False
    if risk_pct > 0 and mult * risk_pct > 0.9:
        sl_usd = STAKE * 0.9
        tp_usd = sl_usd * 3.0
        adj = True
        print(f"   ⚠️ Riesgo tecnico {risk_pct*100:.2f}% muy amplio para mult {mult} -> SL capeado a 90% stake ({sl_usd} USD)")
    else:
        sl_usd = STAKE * mult * risk_pct
        tp_usd = STAKE * mult * rew_pct

    sl_usd, tp_usd = round(sl_usd, 2), round(tp_usd, 2)
    if sl_usd < min_sl_usd:
        print(f"   ⚠️ SL USD {sl_usd} < min {min_sl_usd} para GOLD, ajusto a {min_sl_usd}")
        sl_usd = min_sl_usd; adj = True
    if tp_usd < min_tp_usd:
        tp_usd = max(min_tp_usd, round(sl_usd * 1.5, 2))
        print(f"   ⚠️ TP USD muy chico, ajustado a {tp_usd} USD (1.5x SL, min GOLD) para evitar payout=0")
        adj = True
    if is_gold and tp_usd < sl_usd * 1.5:
        tp_usd = round(sl_usd * 1.5, 2)
        print(f"   ⚠️ Ajusto TP a 1.5x SL para GOLD: {tp_usd} USD")
        adj = True

    return sl_usd, tp_usd, adj, mult

# FIX 2026-10-26: modo SEGUIR SEÑAL (Damaris/UK). Cuando viene de Telegram (SIGNAL_SOURCE definido),
# SL/TP se calculan con el precio REAL (spot) al comprar, para que los precios de Deriv coincidan
# con los de la señal. Los minimos "anti payout=0" (0.5% TP, $5 SL, 1.5x SL) se quitan en este modo
# porque el payout=0 era un falso positivo del script, no un limite de Deriv.
FOLLOW = bool((e("SIGNAL_SOURCE", "") or "").strip())
# Si el precio real de Deriv se alejo mas de este % de la entrada de la señal, NO abre (entraria tarde).
# 0.15% = ~$6 en oro. Cada broker tiene su propio precio, diferencias de $2-3 en oro son normales y pasan.
MAX_SLIP_PCT = float(e("MAX_SLIP_PCT", "0.0015") or 0.0015)

def calc_usd_follow(spot):
    if SIDE not in CONTRACT:
        fail(f"side inválido: {SIDE} (usa BUY o SELL)")
    if SL <= 0 or TP <= 0 or spot <= 0:
        fail(f"Faltan niveles: SPOT={spot} SL={SL} TP={TP}")
    if SIDE == "BUY":
        if spot <= SL:
            fail(f"SEÑAL VENCIDA: precio actual {spot} ya está en/bajo el SL {SL}. No abro.")
        if spot >= TP:
            fail(f"SEÑAL VENCIDA: precio actual {spot} ya llegó al TP {TP}. No abro.")
    else:
        if spot >= SL:
            fail(f"SEÑAL VENCIDA: precio actual {spot} ya está en/sobre el SL {SL}. No abro.")
        if spot <= TP:
            fail(f"SEÑAL VENCIDA: precio actual {spot} ya llegó al TP {TP}. No abro.")
    ALLOWED_MULTS = [100, 200, 300, 500, 800, 1000]
    mult = MULTIPLIER if MULTIPLIER in ALLOWED_MULTS else 100
    risk_pct = abs(spot - SL) / spot
    rew_pct = abs(TP - spot) / spot
    sl_usd = STAKE * mult * risk_pct
    tp_usd = STAKE * mult * rew_pct
    adj = False
    if sl_usd > STAKE * 0.9:
        print(f"   ⚠️ SL equivale a {sl_usd:.2f} USD > 90% del stake -> capeado a {STAKE*0.9:.2f} USD")
        sl_usd = STAKE * 0.9; adj = True
    sl_usd, tp_usd = round(max(sl_usd, MIN_USD), 2), round(max(tp_usd, MIN_USD), 2)
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
        print(f"   {mark} {a.get('account_id')} [{tipo}] saldo={a.get('balance')} {a.get('currency', '')}")
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
    if FOLLOW:
        sl_usd, tp_usd, adj, mult = calc_usd_follow(ENTRY if ENTRY > 0 else (SL + TP) / 2)
        print("   modo: SEGUIR SEÑAL (SL/TP calculados con precio real al comprar)")
    else:
        sl_usd, tp_usd, adj, mult = calc_usd()
    contract = CONTRACT[SIDE]

    print("📋 Orden preparada:")
    print(f"   símbolo     : {DERIV_SYMBOL}")
    print(f"   contrato    : {contract} ({'arriba' if SIDE == 'BUY' else 'abajo'})")
    print(f"   entrada     : {ENTRY}")
    print(f"   SL precio   : {SL}  ->  stop_loss  = {sl_usd} USD")
    print(f"   TP precio   : {TP}  ->  take_profit = {tp_usd} USD  (R:R 1:{tp_usd / sl_usd:.2f})")
    print(f"   stake       : {STAKE} USD × multiplicador {mult:g}")

    if DRY_RUN:
        print("✅ DRY_RUN=1: orden NO enviada (solo simulación).")
        return
    if not API_TOKEN:
        fail("Falta DERIV_API_TOKEN")
    if not APP_ID:
        fail("Falta DERIV_APP_ID")

    ws_url = get_demo_otp_ws()

    proposal = {
        "proposal": 1,
        "amount": STAKE,
        "basis": "stake",
        "contract_type": contract,
        "currency": "USD",
        "multiplier": mult,
        "underlying_symbol": DERIV_SYMBOL,
        "limit_order": {"stop_loss": sl_usd, "take_profit": tp_usd},
    }

    async with websockets.connect(ws_url, open_timeout=20, close_timeout=5) as ws:
        print("✅ ¡WebSocket DEMO conectado! (OTP, sin authorize)")

        if FOLLOW:
            base_prop = {k: v for k, v in proposal.items() if k != "limit_order"}
            rb = await rpc(ws, base_prop, 5)
            if "error" in rb:
                fail(f"proposal(spot): {rb['error'].get('code')} — {rb['error'].get('message')}")
            try:
                live_spot = float(rb.get("proposal", {}).get("spot"))
            except Exception:
                fail(f"No pude leer el spot: {json.dumps(rb)[:300]}")
            if ENTRY > 0 and MAX_SLIP_PCT > 0:
                slip = abs(live_spot - ENTRY) / ENTRY
                if slip > MAX_SLIP_PCT:
                    fail(f"SE ALEJÓ: precio Deriv {live_spot} vs entrada señal {ENTRY} "
                         f"({abs(live_spot-ENTRY):.2f} pts = {slip*100:.3f}% > {MAX_SLIP_PCT*100:.3f}%). No abro para no entrar tarde.")
                print(f"   slippage {abs(live_spot-ENTRY):.2f} pts ({slip*100:.3f}%) dentro del limite {MAX_SLIP_PCT*100:.3f}%")
            sl_usd, tp_usd, adj, mult = calc_usd_follow(live_spot)
            # Compensa la comision de Deriv: el SL cuenta la comision como parte de la perdida (nivel mas cerca)
            # y el TP es ganancia NETA de comision (nivel mas lejos). Verificado con contrato 18419562499:
            # nivel_SL = spot - (monto_SL - comision)*spot/(stake*mult) ; nivel_TP = spot + (monto_TP + comision)*spot/(stake*mult)
            try:
                comm = float(rb.get("proposal", {}).get("commission", 0) or 0)
            except Exception:
                comm = 0.0
            if comm > 0:
                sl_new = min(sl_usd + comm, STAKE * 0.9)
                tp_new = max(tp_usd - comm, MIN_USD)
                print(f"   comision {comm} USD compensada: SL {sl_usd}->{round(sl_new,2)} | TP {tp_usd}->{round(tp_new,2)}")
                sl_usd, tp_usd = round(sl_new, 2), round(tp_new, 2)
            proposal["limit_order"] = {"stop_loss": sl_usd, "take_profit": tp_usd}
            print(f"   spot real {live_spot} (señal decía {ENTRY}) -> SL {SL} = {sl_usd} USD | TP {TP} = {tp_usd} USD")

        r = await rpc(ws, proposal, 2)
        if "error" in r:
            fail(f"proposal: {r['error'].get('code')} — {r['error'].get('message')} — {r['error']}")
        prop = r.get("proposal", {})
        pid = prop.get("id")
        payout = prop.get("payout", "?")
        spot = prop.get("spot", "?")
        print(f"✅ Cotización: id={pid} | payout={payout} | spot={spot}")
        # FIX 2026-10-25: en MULTIPLICADORES el proposal trae payout=0 de forma NORMAL (no hay payout fijo).
        # Antes el script abortaba aqui y NUNCA llegaba a comprar. Ahora solo aborta si no hay id;
        # si la propuesta fuera invalida, Deriv responde error en el buy y se muestra completo.
        print(f"   proposal completo: {json.dumps(prop)[:600]}")
        if not pid:
            fail(f"proposal sin id (spot={spot}) - respuesta: {json.dumps(r)[:400]}")
        try:
            ask = float(prop.get("ask_price", STAKE))
        except Exception:
            ask = STAKE
        buy_price = round(max(STAKE, ask), 2)

        r = await rpc(ws, {"buy": pid, "price": buy_price}, 3)
        if "error" in r:
            fail(f"buy: {r['error'].get('code')} — {r['error'].get('message')}")
        b = r.get("buy", {})
        cid = b.get("contract_id")
        paid = b.get("buy_price", STAKE)
        print(f"🚀 ORDEN EJECUTADA (DEMO): {DERIV_SYMBOL} {contract} | stake={paid} USD | mult={mult:g} | SL={sl_usd} TP={tp_usd} USD | contract_id={cid}")

def main():
    try:
        asyncio.run(place_order())
    except SystemExit:
        raise
    except Exception as ex:
        fail(f"{type(ex).__name__}: {ex}")

if __name__ == "__main__":
    main()
