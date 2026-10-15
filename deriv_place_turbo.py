#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ejecuta TURBO Rise/Fall en DERIV - FIX 2026-10-15: symbol -> underlying_symbol
Contract: CALL (Rise) / PUT (Fall), duracion 1h para R_75
"""
import asyncio, json, os, time, urllib.error, urllib.request
try:
    import websockets
except ImportError:
    print("❌ Falta websockets")
    raise SystemExit(2)

def e(k,d=None): return os.environ.get(k,d)
API_TOKEN=(e("DERIV_API_TOKEN","") or "").strip()
APP_ID=(e("DERIV_APP_ID","") or "").strip()
SIDE=(e("DERIV_SIDE","CALL") or "CALL").upper()  # CALL=Rise, PUT=Fall
SYMBOL=(e("DERIV_SYMBOL","R_100") or "R_100").upper()
STAKE=float(e("STAKE_USD","5") or 5)
DURATION=int(e("DURATION","5") or 5)
DURATION_UNIT=(e("DURATION_UNIT","m") or "m").lower()  # m=min, h=hour

CONTRACT={"CALL":"CALL","PUT":"PUT","RISE":"CALL","FALL":"PUT","BUY":"CALL","SELL":"PUT"}
REST="https://api.derivws.com"

def fail(msg):
    print(f"❌ {msg}")
    raise SystemExit(1)

def rest(method, path, body=None, timeout=20):
    url=REST+path
    headers={"Authorization":f"Bearer {API_TOKEN}","Accept":"application/json","Deriv-App-ID":APP_ID}
    data=None
    if body is not None:
        data=json.dumps(body).encode()
        headers["Content-Type"]="application/json"
    req=urllib.request.Request(url,data=data,headers=headers,method=method)
    try:
        with urllib.request.urlopen(req,timeout=timeout) as resp:
            return resp.getcode(), json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as ex:
        raw=ex.read().decode(errors="replace")
        try: return ex.code, json.loads(raw)
        except: return ex.code, {"raw":raw}
    except Exception as ex: return 0, {"error":str(ex)}

async def rpc(ws,payload,req_id,timeout=30):
    payload=dict(payload); payload["req_id"]=req_id
    await ws.send(json.dumps(payload))
    deadline=time.time()+timeout
    while time.time()<deadline:
        raw=await asyncio.wait_for(ws.recv(),timeout=max(1,deadline-time.time()))
        data=json.loads(raw)
        if data.get("req_id")==req_id: return data
    fail(f"Timeout req_id={req_id}")

def get_demo_otp_ws():
    print(f"🔐 [TURBO DEMO] PAT AppID={APP_ID[:6]}...")
    code,data=rest("GET","/trading/v1/options/accounts")
    if code!=200: fail(f"accounts: {data}")
    accounts=data.get("data") or []
    if isinstance(accounts,dict): accounts=[accounts]
    demo=None
    for a in accounts:
        if a.get("account_type")=="demo" and demo is None: demo=a
    if not demo: fail("No demo")
    acc_id=demo.get("account_id")
    print(f"✅ DEMO: {acc_id} saldo {demo.get('balance')}")
    code,data=rest("POST",f"/trading/v1/options/accounts/{acc_id}/otp")
    if code!=200: fail(f"otp: {data}")
    ws_url=(data.get("data") or {}).get("url")
    if not ws_url: fail(f"otp sin url")
    print(f"✅ OTP listo")
    return ws_url

async def place_order():
    contract=CONTRACT.get(SIDE)
    if not contract: fail(f"SIDE invalido {SIDE} usa CALL/PUT")
    print(f"📋 TURBO {DURATION}{DURATION_UNIT}:")
    print(f"   simbolo: {SYMBOL}")
    print(f"   contrato: {contract} ({SIDE})")
    print(f"   stake: {STAKE} USD {DURATION}{DURATION_UNIT}")
    if not API_TOKEN or not APP_ID: fail("Falta TOKEN/APP_ID")
    ws_url=get_demo_otp_ws()
    # FIX 2026: symbol -> underlying_symbol (breaking change Deriv New API)
    proposal={
        "proposal":1,
        "amount":STAKE,
        "basis":"stake",
        "contract_type":contract,
        "currency":"USD",
        "duration":DURATION,
        "duration_unit":DURATION_UNIT,
        "underlying_symbol":SYMBOL,
    }
    async with websockets.connect(ws_url,open_timeout=20,close_timeout=5) as ws:
        print("✅ WS conectado!")
        r=await rpc(ws,proposal,2)
        if "error" in r:
            fail(f"proposal: {r['error'].get('code')} — {r['error'].get('message')} — {r['error']}")
        prop=r.get("proposal",{})
        pid=prop.get("id")
        payout=prop.get("payout","?")
        spot=prop.get("spot","?")
        print(f"✅ Cotizacion turbo: id={pid} payout={payout} spot={spot}")
        if not pid or payout==0 or payout=="0":
            fail(f"Invalid proposal payout=0 id={pid} spot={spot}")
        r=await rpc(ws,{"buy":pid,"price":STAKE},3)
        if "error" in r:
            fail(f"buy: {r['error'].get('code')} — {r['error'].get('message')}")
        b=r.get("buy",{})
        cid=b.get("contract_id")
        print(f"🚀 TURBO EJECUTADO (DEMO): {SYMBOL} {contract} {STAKE} USD cid={cid} | contract_id={cid}")

def main():
    try: asyncio.run(place_order())
    except SystemExit: raise
    except Exception as ex: fail(f"{type(ex).__name__}: {ex}")

if __name__=="__main__":
    main()
