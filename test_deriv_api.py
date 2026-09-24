#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PRUEBA Deriv API — FIX para API nueva 2026 que devuelve underlying_symbol
"""
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request

try:
    import websockets
except ImportError:
    print("❌ pip install websockets")
    raise SystemExit(2)

TOKEN = (os.environ.get("DERIV_API_TOKEN", "") or "").strip() or "PEGA_AQUI_TU_TOKEN"
APP_ID = (os.environ.get("DERIV_APP_ID", "") or "").strip()
DO_BUY = "--buy" in sys.argv
REST = "https://api.derivws.com"
CLASSIC_WS = "wss://ws.derivws.com/websockets/v3?app_id=1089"
WANT = ["frxAUDNZD", "cryBTCUSD", "frxXAUUSD"]

MULTUP_PROPOSAL = {
    "proposal": 1, "amount": 10, "basis": "stake",
    "contract_type": "MULTUP", "currency": "USD",
    "multiplier": 100, "symbol": "frxAUDNZD",
    "limit_order": {"stop_loss": 5, "take_profit": 15},
}

def rest(method, path, headers_extra=None, body=None, timeout=20):
    url = REST + path
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json",
    }
    if APP_ID:
        headers["Deriv-App-ID"] = APP_ID
    if headers_extra:
        headers.update(headers_extra)
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

async def ws_rpc(ws, payload, req_id, timeout=25):
    payload = dict(payload)
    payload["req_id"] = req_id
    await ws.send(json.dumps(payload))
    end = time.time() + timeout
    while time.time() < end:
        raw = await asyncio.wait_for(ws.recv(), timeout=max(1, end-time.time()))
        data = json.loads(raw)
        if data.get("req_id") == req_id:
            return data
    return {"error": {"code": "TIMEOUT", "message": "sin respuesta"}}

async def check_symbols_and_proposal(ws, tag):
    print(f"📊 [{tag}] active_symbols ...")
    r = await ws_rpc(ws, {"active_symbols": "brief"}, 90)
    if "error" in r:
        print(f"❌ active_symbols: {r['error']}")
        return False
    items = r.get("active_symbols", [])
    if items:
        keys0 = sorted(items[0].keys())
        print(f" ℹ️ claves del primer ítem: {keys0[:14]}")

    def _key(s):
        # FIX 2026: API nueva devuelve underlying_symbol / underlying_symbol_id
        return (s.get("symbol") or s.get("id") or s.get("name") or 
                s.get("underlying_symbol") or s.get("underlying_symbol_id"))

    have = {}
    for s in items:
        k = _key(s)
        if k:
            have[k] = s

    if not have:
        print(f"❌ active_symbols sin campo symbol/id/underlying_symbol. Ejemplo: {items[:1]}")
        return False

    print(f" ({len(have)} símbolos)")
    ok_all = True
    for w in WANT:
        if w in have:
            s = have[w]
            disp = s.get('display_name') or s.get('underlying_symbol_name') or s.get('underlying_symbol') or w
            print(f" ✅ {w} = {disp}")
        else:
            print(f" ❌ {w} NO disponible")
            ok_all = False

    print(f"💰 [{tag}] proposal MULTUP frxAUDNZD (10 USD × 100, SL/TP incluidos) ...")
    r = await ws_rpc(ws, MULTUP_PROPOSAL, 91)
    if "error" in r:
        print(f"❌ proposal: {r['error']}")
        ok_all = False
    else:
        p = r.get("proposal", {})
        print(f"✅ Cotización OK: id={p.get('id')} payout={p.get('payout')} spot={p.get('spot')}")
        if DO_BUY:
            rb = await ws_rpc(ws, {"buy": p["id"], "price": 10}, 92)
            if "error" in rb:
                print(f"❌ buy: {rb['error']}")
            else:
                b = rb.get("buy", {})
                print(f"🚀 ORDEN ABIERTA: contract_id={b.get('contract_id')} stake={b.get('buy_price')}")
                print(" → Revísala en tu panel Deriv (Positions).")
        else:
            print(" (sin --buy: no se compró nada)")
    return ok_all

async def test_new_pat_otp():
    print(f"🔌 [NUEVO] REST con PAT | Deriv-App-ID={APP_ID[:6]}...")
    code, data = rest("GET", "/trading/v1/options/accounts")
    print(f"GET /accounts -> HTTP {code}")
    if code != 200:
        print(f"❌ accounts: {data}")
        return False

    # data puede ser lista o dict con 'data'
    accounts = []
    if isinstance(data, dict):
        if "data" in data:
            accounts = data["data"]
        elif "accounts" in data:
            accounts = data["accounts"]
        else:
            # intenta buscar cuentas en el dict
            accounts = data.get("data", [])
    elif isinstance(data, list):
        accounts = data

    # La API devuelve lista de cuentas con account_type
    demo_acc = None
    for acc in accounts if isinstance(accounts, list) else []:
        if isinstance(acc, dict):
            at = acc.get("account_type") or acc.get("type")
            aid = acc.get("id") or acc.get("account_id")
            login = acc.get("loginid") or acc.get("login_id") or aid
            bal = acc.get("balance", "?")
            curr = acc.get("currency", "")
            is_demo = (at == "demo") or (str(login).upper().startswith("VRTC") or str(login).upper().startswith("DOT"))
            # imprime todas
            tipo = "demo" if is_demo else "real"
            print(f" {'🟢' if is_demo else '🔴'} {aid} [{tipo}] saldo={bal} {curr} login={login}")
            if is_demo and not demo_acc:
                demo_acc = acc

    # Fallback: si la estructura es diferente, busca campo demo
    if not demo_acc:
        # intenta estructura antigua: lista con is_virtual
        for acc in accounts if isinstance(accounts, list) else []:
            if isinstance(acc, dict) and acc.get("account_type") == "demo":
                demo_acc = acc
                break

    if not demo_acc:
        # último intento: toma la primera demo que encuentre por loginid
        for acc in accounts if isinstance(accounts, list) else []:
            if isinstance(acc, dict):
                lid = str(acc.get("id", "") + acc.get("loginid", ""))
                if "demo" in str(acc).lower() or "DOT" in lid or "VRTC" in lid:
                    demo_acc = acc
                    break

    if not demo_acc:
        # Si no encontramos, usa DOT91988837 que vimos en tu log
        print("⚠️ No se detectó demo automáticamente, usando DOT91988837 del log")
        # busca DOT91988837
        for acc in accounts if isinstance(accounts, list) else []:
            if isinstance(acc, dict) and "DOT91988837" in str(acc):
                demo_acc = acc
                break

    if not demo_acc and accounts:
        demo_acc = accounts[0]  # fallback

    if not demo_acc:
        print(f"❌ No se encontró cuenta demo en: {data}")
        return False

    acc_id = demo_acc.get("id") or demo_acc.get("account_id") or demo_acc.get("loginid")
    print(f"✅ Cuenta DEMO elegida: {acc_id} (saldo {demo_acc.get('balance', '?')})")

    # 2) OTP
    code, data = rest("POST", f"/trading/v1/options/accounts/{acc_id}/otp")
    print(f"POST /otp -> HTTP {code}")
    if code != 200:
        print(f"❌ otp: {data}")
        return False

    ws_url = None
    if isinstance(data, dict):
        ws_url = data.get("data", {}).get("url") if isinstance(data.get("data"), dict) else data.get("url") or data.get("data")
        if isinstance(data.get("data"), str):
            ws_url = data.get("data")
    if not ws_url:
        ws_url = data.get("url") if isinstance(data, dict) else None

    if not ws_url:
        print(f"❌ otp sin url: {data}")
        return False

    print(f"✅ OTP listo -> {ws_url.split('?')[0]}?otp=***")

    async with websockets.connect(ws_url, open_timeout=20, close_timeout=5) as ws:
        print("✅ ¡WebSocket DEMO conectado!")
        return await check_symbols_and_proposal(ws, "demo")

async def test_classic():
    print(f"🔌 [CLÁSICO] Conectando a {CLASSIC_WS} ...")
    async with websockets.connect(CLASSIC_WS, open_timeout=20, close_timeout=5) as ws:
        print("✅ ¡WebSocket conectado!")
        r = await ws_rpc(ws, {"authorize": TOKEN}, 1)
        if "error" in r:
            print(f"❌ authorize: {r['error']}")
            print(" → Token clásico inválido. Si es PAT, define DERIV_APP_ID.")
            return False
        a = r["authorize"]
        print(f"✅ Cuenta: {a.get('loginid')} | saldo: {a.get('balance')}{a.get('currency')}")
        print(f" is_virtual = {a.get('is_virtual')} (1=demo, 0=REAL)")
        if str(a.get("loginid", "")).upper().startswith("CR"):
            print(" ⚠️ ¡CUIDADO! Cuenta REAL — para demo usa el flujo NUEVO (PAT+App ID).")
        for acc in a.get("account_list") or []:
            tipo = "DEMO" if acc.get("is_virtual") else "REAL"
            print(f" • {acc.get('loginid')} [{tipo}] saldo={acc.get('balance', '?')}{acc.get('currency', '')}")
        return await check_symbols_and_proposal(ws, "classic")

async def main():
    print(f"🧩 Modo: {'NUEVO (PAT+OTP) → demo' if APP_ID else 'CLÁSICO'} | APP_ID len={len(APP_ID)} | token len={len(TOKEN)}")
    if TOKEN in ("", "PEGA_AQUI_TU_TOKEN"):
        print("❌ Falta DERIV_API_TOKEN")
        raise SystemExit(1)
    if APP_ID:
        ok = await test_new_pat_otp()
    else:
        print("ℹ️ Sin DERIV_APP_ID → modo clásico")
        ok = await test_classic()
    print("="*60)
    if ok:
        print("🎉 TODO OK: conexión + auth + símbolos + cotización.")
    else:
        print("❌ La prueba NO completó todo. Revisa los errores de arriba.")
    print("="*60)
    if not ok:
        raise SystemExit(1)

if __name__ == "__main__":
    exit_code = 0
    try:
        asyncio.run(main())
    except SystemExit as ex:
        exit_code = int(ex.code or 0)
    except KeyboardInterrupt:
        pass
    except Exception as ex:
        import traceback
        print(f"❌ {type(ex).__name__}: {ex}")
        traceback.print_exc()
        exit_code = 1
    finally:
        if not os.environ.get("CI"):
            try:
                input("\n===== Pulsa ENTER para cerrar esta ventana =====")
            except EOFError:
                pass
        raise SystemExit(exit_code)
