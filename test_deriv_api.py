#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
  PRUEBA Deriv API — SIN comprar. Detecta el modo automáticamente:

  MODO NUEVO (PAT + OTP)  → si existe DERIV_APP_ID
     1) GET  /trading/v1/options/accounts        (lista cuentas: demo/real)
     2) elige la cuenta account_type == "demo"
     3) POST /trading/v1/options/accounts/{id}/otp  (¡aquí se elige la demo!)
     4) conecta al WebSocket wss://.../ws/demo?otp=...
     5) active_symbols + cotización MULTUP con SL/TP

  MODO CLÁSICO            → si NO hay DERIV_APP_ID
     authorize en ws.derivws.com (como antes; muestra account_list + is_virtual)

  Variables de entorno:
    DERIV_API_TOKEN   PAT (nuevo) o token clásico
    DERIV_APP_ID      App ID de developers.deriv.com (necesario para PAT)
    DERIV_DRY_RUN     1 = no comprar nada extra (por ahora nunca compra)

  Uso: python test_deriv_api.py [--buy]   (--buy: compra de prueba en demo)
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
    """Llamada REST simple con urllib (sin dependencias extra)."""
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
        raw = await asyncio.wait_for(ws.recv(), timeout=max(1, end - time.time()))
        data = json.loads(raw)
        if data.get("req_id") == req_id:
            return data
    return {"error": {"code": "TIMEOUT", "message": "sin respuesta"}}


async def check_symbols_and_proposal(ws, tag):
    """active_symbols + cotización MULTUP en una conexión WS ya abierta."""
    print(f"📊 [{tag}] active_symbols ...")
    r = await ws_rpc(ws, {"active_symbols": "brief"}, 90)
    if "error" in r:
        print(f"❌ active_symbols: {r['error']}")
        return False
    have = {s["symbol"]: s for s in r.get("active_symbols", [])}
    print(f"   ({len(have)} símbolos)")
    ok_all = True
    for w in WANT:
        if w in have:
            s = have[w]
            print(f"   ✅ {w} = {s.get('display_name')}")
        else:
            print(f"   ❌ {w} NO disponible")
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
                print(f"🚀 ORDEN ABIERTA: contract_id={b.get('contract_id')} "
                      f"stake={b.get('buy_price')}")
                print("   → Revísala en tu panel Deriv (Positions).")
        else:
            print("   (sin --buy: no se compró nada)")
    return ok_all


async def test_new_pat_otp():
    """Flujo oficial 2026: PAT → GET accounts → OTP (demo) → WS demo."""
    print(f"🔐 [NUEVO] REST con PAT | Deriv-App-ID={APP_ID[:6]}...")

    # 1) listar cuentas de la Options API
    code, data = rest("GET", "/trading/v1/options/accounts")
    print(f"   GET /accounts -> HTTP {code}")
    if code != 200:
        errs = data.get("errors") or data
        print(f"❌ accounts: {errs}")
        if code == 401:
            print("   → Token inválido, o falta Deriv-App-ID (crea App + PAT correctos).")
        return False
    accounts = data.get("data") or []
    if isinstance(accounts, dict):
        accounts = [accounts]
    if not accounts:
        print("   ⚠️ Sin cuentas Options. Creando demo vía POST /accounts ...")
        code, data = rest("POST", "/trading/v1/options/accounts",
                          body={"currency": "USD"})
        print(f"   POST /accounts -> HTTP {code}: {data}")
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
        print("❌ No hay cuenta account_type='demo'. No sigo (solo demo).")
        return False
    acc_id = demo.get("account_id")
    print(f"✅ Cuenta DEMO elegida: {acc_id} (saldo {demo.get('balance')})")

    # 2) OTP para ESA cuenta (aquí se "selecciona" la demo)
    code, data = rest("POST", f"/trading/v1/options/accounts/{acc_id}/otp")
    print(f"   POST /otp -> HTTP {code}")
    if code != 200:
        print(f"❌ otp: {data.get('errors') or data}")
        return False
    ws_url = (data.get("data") or {}).get("url")
    if not ws_url:
        print(f"❌ otp sin url: {data}")
        return False
    print(f"✅ OTP listo -> {ws_url.split('?')[0]}?otp=***")

    # 3) conectar al WS demo (sin authorize: el OTP ya autentica)
    async with websockets.connect(ws_url, open_timeout=20, close_timeout=5) as ws:
        print("✅ ¡WebSocket DEMO conectado!")
        return await check_symbols_and_proposal(ws, "demo")


async def test_classic():
    """Flujo clásico: authorize en ws.derivws.com/v3."""
    print(f"🔌 [CLÁSICO] Conectando a {CLASSIC_WS} ...")
    async with websockets.connect(CLASSIC_WS, open_timeout=20, close_timeout=5) as ws:
        print("✅ ¡WebSocket conectado!")
        r = await ws_rpc(ws, {"authorize": TOKEN}, 1)
        if "error" in r:
            print(f"❌ authorize: {r['error']}")
            print("   → Token clásico inválido. Si es PAT, define DERIV_APP_ID.")
            return False
        a = r["authorize"]
        print(f"✅ Cuenta: {a.get('loginid')} | saldo: {a.get('balance')} {a.get('currency')}")
        print(f"   is_virtual = {a.get('is_virtual')}   (1=demo, 0=REAL)")
        if str(a.get("loginid", "")).upper().startswith("CR"):
            print("   ⚠️ ¡CUIDADO! Cuenta REAL — para demo usa el flujo NUEVO (PAT+App ID).")
        for acc in a.get("account_list") or []:
            tipo = "DEMO" if acc.get("is_virtual") else "REAL"
            print(f"   • {acc.get('loginid')} [{tipo}] "
                  f"saldo={acc.get('balance', '?')} {acc.get('currency', '')}")
        return await check_symbols_and_proposal(ws, "classic")


async def main():
    print(f"🧩 Modo: {'NUEVO (PAT+OTP) → demo' if APP_ID else 'CLÁSICO'} | "
          f"APP_ID len={len(APP_ID)} | token len={len(TOKEN)}")
    if TOKEN in ("", "PEGA_AQUI_TU_TOKEN"):
        print("❌ Falta DERIV_API_TOKEN (PAT en developers.deriv.com o token clásico).")
        raise SystemExit(1)
    if APP_ID:
        ok = await test_new_pat_otp()
    else:
        print("ℹ️ Sin DERIV_APP_ID → modo clásico (NO entra a la demo; solo diagnóstico).")
        ok = await test_classic()
    print("=" * 60)
    if ok:
        print("🎉 TODO OK: conexión + auth + símbolos + cotización.")
    else:
        print("❌ La prueba NO completó todo. Revisa los errores de arriba.")
    print("=" * 60)
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
