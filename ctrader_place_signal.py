#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
  Ejecuta la orden en cTrader (Open API) según la señal elegida por senal_diaria.py.
  Se invoca como SUBPROCESO (cada vez conecta, coloca la orden y sale).
  Lee la orden de variables de entorno: CTRADER_SIDE / CTRADER_SYMBOL /
  CTRADER_VOLUME / SIGNAL_SL / SIGNAL_TP.
  Usa la cuenta simulada (DEMO) por defecto.
"""
import os

from twisted.internet import reactor
from ctrader_open_api import Client, EndPoints, Protobuf
from ctrader_open_api.tcpProtocol import TcpProtocol
from ctrader_open_api.messages import OpenApiMessages_pb2 as M
from ctrader_open_api.messages import OpenApiModelMessages_pb2 as MM

def e(key, default=None):
    return os.environ.get(key, default)

CLIENT_ID     = e("CTRADER_CLIENT_ID", "").strip()
CLIENT_SECRET = e("CTRADER_CLIENT_SECRET", "").strip()
ACCESS_TOKEN  = e("CTRADER_ACCESS_TOKEN", "").strip()
ACCOUNT_ID    = e("CTRADER_ACCOUNT_ID", "").strip()
ENV           = (e("CTRADER_ENV", "demo") or "demo").lower()

SIDE          = (e("CTRADER_SIDE", "BUY") or "BUY").upper()
SYMBOL        = (e("CTRADER_SYMBOL", "EURUSD") or "EURUSD").upper()
VOLUME        = float(e("CTRADER_VOLUME", "0.1"))
SIGNAL_SL     = float(e("SIGNAL_SL", "0") or 0) or None
SIGNAL_TP     = float(e("SIGNAL_TP", "0") or 0) or None
DECIMALS      = int(e("CTRADER_DECIMALS", "5"))

HOST = EndPoints.PROTOBUF_DEMO_HOST if ENV == "demo" else EndPoints.PROTOBUF_LIVE_HOST
PORT = EndPoints.PROTOBUF_PORT

TRADE_SIDE = {"BUY": MM.ProtoOATradeSide.BUY, "SELL": MM.ProtoOATradeSide.SELL}
if SIDE not in TRADE_SIDE:
    print(f"❌ side inválido: {SIDE}"); raise SystemExit(2)

state = {"symbol_id": None, "done": False}

def fail(msg):
    print(f"❌ {msg}")
    state["done"] = True
    try: reactor.stop()
    except Exception: pass

def success(msg):
    print(f"✅ {msg}")
    state["done"] = True
    try: reactor.stop()
    except Exception: pass

def on_message(client, message):
    payload = Protobuf.extract(message)

    if isinstance(payload, M.ProtoOAApplicationAuthRes):
        if payload.errorCode in (0, None, ""):
            print("✅ App autenticada.")
            _auth_account(client)
        else:
            fail(f"Error auth app: {payload.errorCode}")

    elif isinstance(payload, M.ProtoOAAccountAuthRes):
        if payload.errorCode in (0, None, ""):
            print(f"✅ Cuenta {ACCOUNT_ID} autenticada.")
            _get_symbols(client)
        else:
            fail(f"Error auth cuenta: {payload.errorCode}")

    elif isinstance(payload, M.ProtoOASymbolsListRes):
        for s in payload.symbol:
            if s.name.upper() == SYMBOL:
                state["symbol_id"] = s.symbolId
                print(f"✅ Símbolo {SYMBOL} (id={s.symbolId})")
                break
        if state["symbol_id"] is None:
            fail(f"No encontré el símbolo {SYMBOL} en tu cuenta.")
        else:
            _open_order(client)

    elif isinstance(payload, M.ProtoOAExecutionEvent):
        et = payload.executionType
        if et == MM.ProtoOAExecutionType.ORDER_ACCEPTED:
            print("📄 Orden aceptada...")
        elif et == MM.ProtoOAExecutionType.ORDER_FILLED:
            success(f"ORDEN EJECUTADA: {SYMBOL} {SIDE} {VOLUME} "
                    f"@ {payload.price} | positionId={payload.positionId}")
        elif et == MM.ProtoOAExecutionType.ORDER_REJECTED:
            fail(f"Orden RECHAZADA: {payload.rejectionReason}")

def _auth_account(client):
    req = M.ProtoOAAccountAuthReq()
    req.ctidTraderAccountId = int(ACCOUNT_ID)
    req.accessToken = ACCESS_TOKEN
    client.send(req)

def _get_symbols(client):
    req = M.ProtoOASymbolsListReq()
    req.ctidTraderAccountId = int(ACCOUNT_ID)
    client.send(req)

def _open_order(client):
    req = M.ProtoOANewOrderReq()
    req.ctidTraderAccountId = int(ACCOUNT_ID)
    req.symbolId = state["symbol_id"]
    req.orderType = MM.ProtoOAOrderType.MARKET
    req.tradeSide = TRADE_SIDE[SIDE]
    req.volume = VOLUME
    req.timeInForce = MM.ProtoOATimeInForce.GOOD_TILL_CANCEL
    req.comment = "Senal diaria"
    req.baseSlippagePrice = 0
    req.slippageInPoints = 0
    if SIGNAL_TP:
        req.takeProfit = round(SIGNAL_TP, DECIMALS)
    if SIGNAL_SL:
        req.stopLoss = round(SIGNAL_SL, DECIMALS)
    print(f"🚀 Enviando orden: {SIDE} {VOLUME} {SYMBOL} (mercado) "
          f"SL={SIGNAL_SL} TP={SIGNAL_TP}")
    client.send(req)

def run():
    if not (CLIENT_ID and CLIENT_SECRET and ACCESS_TOKEN and ACCOUNT_ID):
        print("❌ Faltan credenciales cTrader. Revisa variables de entorno.")
        return
    client = Client(HOST, PORT, TcpProtocol)
    client.setMessageReceivedCallback(on_message)

    def _connected(c):
        print(f"🔌 Conectado a {HOST}:{PORT} ({ENV})")
        req = M.ProtoOAApplicationAuthReq()
        req.clientId = CLIENT_ID
        req.clientSecret = CLIENT_SECRET
        c.send(req)

    client.setConnectedCallback(_connected)
    client.setDisconnectedCallback(lambda _c, r: print("🔌 Desconectado:", r))
    client.startService()
    reactor.run()

if __name__ == "__main__":
    run()
