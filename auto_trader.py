#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
═══════════════════════════════════════════════════════════════════════════════
  AUTO-TRADER EURUSD — Vigila cTrader y ABRE la operación automáticamente
═══════════════════════════════════════════════════════════════════════════════
  Qué hace:
    1) Se conecta a la cTrader Open API y AUTHENTICA tu app + cuenta.
    2) Se SUSCRIBE a los precios EN VIVO de EURUSD (del propio broker).
    3) Vigila continuamente si el precio entra en tu zona de entrada.
    4) Cuando se cumple → ABRE la orden por ti (con SL/TP) y te avisa por Telegram.

  🔑 DIFERENCIA CLAVE con los otros bots:
     El precio se lee del MISMO cTrader (no de Yahoo), así la entrada y la
     ejecución van al mismo precio del broker. Eso es lo correcto para operar.

  ⚠️ ADVERTENCIA SERIA:
     Esto abRE operaciones de dinero REAL sin tu confirmación. Por eso:
       • Prueba SIEMPRE con CTRADER_ENV="demo".
       • Usa TRADE_AUTOMATIC=true SOLO cuando lo domines.
       • Es tu responsabilidad, NO es asesoría financiera.

  CREDENCIALES (variables de entorno):
    CTRADER_CLIENT_ID, CTRADER_CLIENT_SECRET, CTRADER_ACCESS_TOKEN,
    CTRADER_ACCOUNT_ID, CTRADER_ENV (demo/live)
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (para avisarte)

  INSTALAR:
    pip install ctrader-open-api requests

  EJECUTAR:
    export CTRADER_CLIENT_ID="..." ; export CTRADER_CLIENT_SECRET="..."
    export CTRADER_ACCESS_TOKEN="..." ; export CTRADER_ACCOUNT_ID="..."
    export CTRADER_ENV="demo"
    export TELEGRAM_BOT_TOKEN="..." ; export TELEGRAM_CHAT_ID="..."
    python3 auto_trader.py
═══════════════════════════════════════════════════════════════════════════════
"""

import os
import time
import requests

from twisted.internet import reactor

from ctrader_open_api import Client, EndPoints, Protobuf
from ctrader_open_api.tcpProtocol import TcpProtocol
from ctrader_open_api.messages import OpenApiMessages_pb2 as M
from ctrader_open_api.messages import OpenApiModelMessages_pb2 as MM

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ──────────────────────────────────────────────────────────────────────────────
def e(key, default=None):
    return os.environ.get(key, default)

CLIENT_ID     = e("CTRADER_CLIENT_ID", "").strip()
CLIENT_SECRET = e("CTRADER_CLIENT_SECRET", "").strip()
ACCESS_TOKEN  = e("CTRADER_ACCESS_TOKEN", "").strip()
ACCOUNT_ID    = e("CTRADER_ACCOUNT_ID", "").strip()
ENV           = (e("CTRADER_ENV", "demo") or "demo").lower()
SYMBOL        = (e("CTRADER_SYMBOL", "EURUSD") or "EURUSD").upper()

TG_TOKEN      = e("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT       = e("TELEGRAM_CHAT_ID", "").strip()

# ── TU SEÑAL (los parámetros de entrada que TÚ me das) ──
DIRECCION = (e("DIRECCION", "BUY") or "BUY").upper()   # BUY o SELL
ENTRADA   = float(e("ENTRADA", "1.16000"))             # punto de entrada
TP        = float(e("TP", "1.16370"))
SL        = float(e("SL", "1.15680"))
VOLUMEN   = float(e("VOLUMEN", "0.1"))                 # lotes
TOLERANCIA = float(e("TOLERANCIA", "0.0008"))          # ± pips de la entrada

# ── Seguridad ──
# true = abre solo al cumplirse la entrada (lo que pediste).
# false = NO abre, solo te manda el aviso "entrada alcanzada" (modo vigilante).
TRADE_AUTOMATIC = e("TRADE_AUTOMATIC", "true").lower() in ("1", "true", "yes")
# Máximo de operaciones que abrirá automáticamente (evita bucle infinito).
MAX_AUTO_TRADES = int(e("MAX_AUTO_TRADES", "1"))

HOST = EndPoints.PROTOBUF_DEMO_HOST if ENV == "demo" else EndPoints.PROTOBUF_LIVE_HOST
PORT = EndPoints.PROTOBUF_PORT

# Estado
state = {
    "symbol_id": None,
    "authed": False,
    "spot_count": 0,
    "open_count": 0,
    "entry_triggered": False,     # ya se disparó la condición de entrada
    "open_now": False,            # mandar la orden en este tick
}

BOUGHT  = MM.ProtoOATradeSide.BUY
SOLD    = MM.ProtoOATradeSide.SELL
SIDE_VAL = BOUGHT if DIRECCION == "BUY" else SOLD

# ──────────────────────────────────────────────────────────────────────────────
# TELEGRAM (avisos)
# ──────────────────────────────────────────────────────────────────────────────
def tg(text):
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
                      timeout=15)
    except Exception as ex:
        print("[Telegram] error:", ex)

# ──────────────────────────────────────────────────────────────────────────────
# FLUJO DE MENSAJES
# ──────────────────────────────────────────────────────────────────────────────
def on_message(client, message):
    payload = Protobuf.extract(message)

    if isinstance(payload, M.ProtoOAApplicationAuthRes):
        if payload.errorCode in (0, None, ""):
            print("✅ App autenticada.")
            _auth_account(client)
        else:
            _fail(f"Error auth app: {payload.errorCode}")

    elif isinstance(payload, M.ProtoOAAccountAuthRes):
        if payload.errorCode in (0, None, ""):
            print(f"✅ Cuenta {ACCOUNT_ID} autenticada.")
            _get_symbols(client)
        else:
            _fail(f"Error auth cuenta: {payload.errorCode}")

    elif isinstance(payload, M.ProtoOASymbolsListRes):
        for s in payload.symbol:
            if s.name.upper() == SYMBOL:
                state["symbol_id"] = s.symbolId
                print(f"✅ Símbolo {SYMBOL} (id={s.symbolId})")
                break
        if state["symbol_id"] is None:
            _fail(f"No encontré {SYMBOL}.")
        else:
            _subscribe(client)

    elif isinstance(payload, M.ProtoOASubscribeSpotsRes):
        print("✅ Suscrito a precios en vivo. Vigilando...")
        tg(f"🟢 <b>Auto-trader activo.</b>\n"
           f"📌 Señal: <b>{DIRECCION}</b> en {ENTRADA:.5f}\n"
           f"🎯 TP: {TP:.5f} | 🛑 SL: {SL:.5f}\n"
           f"📦 Vol: {VOLUMEN} | 💾 {('DEMO' if ENV=='demo' else 'REAL')}\n"
           f"⚙️ Automático: {'SÍ' if TRADE_AUTOMATIC else 'NO (solo aviso)'}")

    elif isinstance(payload, M.ProtoOASpotEvent):
        state["spot_count"] += 1
        ask = payload.ask
        bid = payload.bid
        _on_spot(ask, bid, client)

    elif isinstance(payload, M.ProtoOAExecutionEvent):
        _on_execution(payload)

def _auth_account(client):
    req = M.ProtoOAAccountAuthReq()
    req.ctidTraderAccountId = int(ACCOUNT_ID)
    req.accessToken = ACCESS_TOKEN
    client.send(req)

def _get_symbols(client):
    req = M.ProtoOASymbolsListReq()
    req.ctidTraderAccountId = int(ACCOUNT_ID)
    client.send(req)

def _subscribe(client):
    req = M.ProtoOASubscribeSpotsReq()
    req.ctidTraderAccountId = int(ACCOUNT_ID)
    req.symbolId = state["symbol_id"]
    req.subscribeToSpotTimestamp = 0
    client.send(req)

# ── LÓGICA DE ENTRADA ──
def _on_spot(ask, bid, client):
    # Para COMPRA (BUY) se llena al ASK; para VENTA (SELL) al BID.
    ref = ask if DIRECCION == "BUY" else bid
    zmin = ENTRADA - TOLERANCIA
    zmax = ENTRADA + TOLERANCIA
    inside = zmin <= ref <= zmax

    if state["spot_count"] % 20 == 0 or inside:
        print(f"   {SYMBOL} ask={ask:.5f} bid={bid:.5f}  "
              f"{'🎯 EN ZONA' if inside else ''}")

    if inside and not state["entry_triggered"]:
        state["entry_triggered"] = True
        print(f"🎯 ¡Entrada alcanzada! ref={ref:.5f}")
        if TRADE_AUTOMATIC and state["open_count"] < MAX_AUTO_TRADES:
            _open_order(client, ref)
        else:
            tg(f"🎯 <b>¡ENTRADA ALCANZADA!</b>\n{ref:.5f}\n"
               f"(modo aviso; no abrí por config)")

def _open_order(client, ref):
    req = M.ProtoOANewOrderReq()
    req.ctidTraderAccountId = int(ACCOUNT_ID)
    req.symbolId = state["symbol_id"]
    req.orderType = MM.ProtoOAOrderType.MARKET
    req.tradeSide = SIDE_VAL
    req.volume = VOLUMEN
    req.timeInForce = MM.ProtoOATimeInForce.GOOD_TILL_CANCEL
    req.comment = "Auto-trader"
    req.baseSlippagePrice = 0
    req.slippageInPoints = 0
    if SL:
        req.stopLoss = round(SL, 5)
    if TP:
        req.takeProfit = round(TP, 5)
    print(f"🚀 Abriendo: {DIRECCION} {VOLUMEN} {SYMBOL} @ mercado "
          f"(ref={ref:.5f}) SL={SL} TP={TP}")
    client.send(req)
    tg(f"🚀 <b>Abriendo operación en cTrader...</b>\n"
       f"📌 {DIRECCION} {VOLUMEN} {SYMBOL} (mercado)\n"
       f"🎯 TP: {TP:.5f} | 🛑 SL: {SL:.5f}")

def _on_execution(payload):
    et = payload.executionType
    if et == MM.ProtoOAExecutionType.ORDER_ACCEPTED:
        print("📄 Orden aceptada...")
        tg("📄 Orden aceptada en cTrader, esperando ejecución.")
    elif et == MM.ProtoOAExecutionType.ORDER_FILLED:
        state["open_count"] += 1
        print(f"✅ ORDEN EJECUTADA: {payload.price:.5f} positionId={payload.positionId}")
        tg(f"✅ <b>¡OPERACIÓN ABIERTA!</b>\n"
           f"💱 Precio de ejecución: <b>{payload.price:.5f}</b>\n"
           f"🎯 TP: {TP:.5f} | 🛑 SL: {SL:.5f}\n"
           f"Posición: {payload.positionId}")
        if state["open_count"] >= MAX_AUTO_TRADES:
            print("🏁 Máximo de operaciones alcanzado. Deteniendo.")
            tg("🏁 Se alcanzó el máximo de operaciones automáticas. Salgo.")
            _stop()
    elif et == MM.ProtoOAExecutionType.ORDER_REJECTED:
        _fail(f"Orden RECHAZADA: {payload.rejectionReason}")

def _fail(msg):
    print("❌", msg)
    tg(f"❌ <b>Error:</b> {msg}")
    _stop()

def _stop():
    try:
        reactor.stop()
    except Exception:
        pass

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
