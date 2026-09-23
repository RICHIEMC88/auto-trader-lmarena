#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
  PRUEBA Deriv API — verifica conexión + símbolos + cotización, SIN comprar.
  (Si pasas --buy, pone una orden real de 1 USD en demo como prueba final.)

  Uso:
    1) pip install websockets
    2) set DERIV_API_TOKEN=xxxxxx        (Windows: en cmd, o edita abajo)
    3) python test_deriv_api.py          -> solo prueba
       python test_deriv_api.py --buy    -> compra de prueba (demo)

  También puedes pegar el token aquí:
"""
import asyncio
import json
import sys
import time

try:
    import websockets
except ImportError:
    print("❌ pip install websockets"); raise SystemExit(2)

# ── PEGA TU TOKEN AQUÍ (o usa la variable de entorno DERIV_API_TOKEN) ────────
import os
TOKEN = os.environ.get("DERIV_API_TOKEN", "").strip() or "PEGA_AQUI_TU_TOKEN"
# ─────────────────────────────────────────────────────────────────────────────

APP_ID = "1089"
URL = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
WANT = ["frxAUDNZD", "cryBTCUSD", "frxXAUUSD"]
DO_BUY = "--buy" in sys.argv


async def rpc(ws, payload, req_id, timeout=25):
    payload = dict(payload); payload["req_id"] = req_id
    await ws.send(json.dumps(payload))
    end = time.time() + timeout
    while time.time() < end:
        raw = await asyncio.wait_for(ws.recv(), timeout=max(1, end - time.time()))
        data = json.loads(raw)
        if data.get("req_id") == req_id:
            return data
    return {"error": {"code": "TIMEOUT", "message": "sin respuesta"}}


async def main():
    print(f"🔌 Conectando a {URL} ...")
    async with websockets.connect(URL, open_timeout=20, close_timeout=5) as ws:
        print("✅ ¡WebSocket conectado!\n")

        # 1) authorize
        print("🔐 authorize ...")
        r = await rpc(ws, {"authorize": TOKEN}, 1)
        if "error" in r:
            print(f"❌ authorize: {r['error']}")
            print("   → Revisa que el token sea correcto y tenga scopes Read + Trade.")
            return
        a = r["authorize"]
        print(f"✅ Cuenta: {a.get('loginid')} | saldo: {a.get('balance')} {a.get('currency')}")
        if str(a.get("loginid", "")).upper().startswith("CR"):
            print("   ⚠️ ¡CUIDADO! Es cuenta REAL — usa el token de la demo (VRTC...).")
        scopes = a.get("scope", [])
        print(f"   scopes: {scopes}")
        if "trade" not in scopes:
            print("   ❌ El token NO tiene scope 'trade'. Regenera con Read + Trade.")
            return
        print()

        # 2) símbolos
        print("📊 active_symbols ...")
        r = await rpc(ws, {"active_symbols": "brief", "product_type": "basic"}, 2)
        if "error" in r:
            print(f"❌ active_symbols: {r['error']}"); return
        have = {s["symbol"]: s for s in r.get("active_symbols", [])}
        print(f"   ({len(have)} símbolos disponibles)")
        for w in WANT:
            if w in have:
                s = have[w]
                print(f"   ✅ {w} = {s.get('display_name')} (pip={s.get('pip')})")
            else:
                print(f"   ❌ {w} NO disponible en tu cuenta")
        faltan = [w for w in WANT if w not in have]
        print()

        # 3) cotización de prueba (MULTUP AUDNZD con SL/TP) — no compra
        if "frxAUDNZD" in have:
            print("💰 proposal MULTUP frxAUDNZD (stake 10 × mult 100, SL/TP incluidos) ...")
            r = await rpc(ws, {
                "proposal": 1, "amount": 10, "basis": "stake",
                "contract_type": "MULTUP", "currency": "USD",
                "multiplier": 100, "symbol": "frxAUDNZD",
                "limit_order": {"stop_loss": 5, "take_profit": 15},
            }, 3)
            if "error" in r:
                print(f"❌ proposal: {r['error']}")
            else:
                p = r["proposal"]
                print(f"✅ Cotización OK: id={p.get('id')} payout={p.get('payout')} "
                      f"spot={p.get('spot')}")
                if DO_BUY:
                    print("\n🛒 COMPRANDO (orden de prueba real en demo) ...")
                    rb = await rpc(ws, {"buy": p["id"], "price": 10}, 4)
                    if "error" in rb:
                        print(f"❌ buy: {rb['error']}")
                    else:
                        b = rb["buy"]
                        print(f"🚀 ORDEN ABIERTA: contract_id={b.get('contract_id')} "
                              f"stake={b.get('buy_price')}")
                        print("   → Revísala en app.deriv.com → Trading.")
                else:
                    print("   (sin --buy: no se compró nada)")

        print("\n" + "=" * 60)
        if faltan:
            print(f"⚠️ Faltan símbolos: {faltan} — avísame y ajusto el mapeo.")
        else:
            print("🎉 TODO OK: conexión + auth + símbolos + cotización.")
        print("=" * 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as ex:
        print(f"❌ {type(ex).__name__}: {ex}")
        print("   → ¿Estás en México con IP normal? (Este error suele ser "
              "geobloqueo o sin internet al WebSocket de Deriv.)")
