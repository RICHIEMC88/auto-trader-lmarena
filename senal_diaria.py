#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOT DE SEÑAL DIARIA v3 - DOBLE ESTRATEGIA + NOTIFICACION DE CIERRES
- Estrategia 1: PROFESIONAL (SMA/MACD/RSI/Estructura) - la que ya tenias
- Estrategia 2: INSTITUCIONAL / SMART MONEY (Order Blocks + FVG + Liquidez de ballenas)
- Avisa cuando abre Y cuando cierra, mencionando que estrategia uso
- Intervalo 2h

Estrategia INSTITUCIONAL (ballenas):
Las instituciones no compran en cualquier lado. Hacen 3 cosas:
1) BARREN LIQUIDEZ: Llevan el precio a cazar stops por debajo del minimo o por encima del maximo previo (stop hunt)
2) MITIGAN ORDER BLOCK: Vuelven a la ultima vela contraria antes del impulso (donde dejaron ordenes pendientes)
3) DEJAN FVG (Fair Value Gap / Imbalance): Hueco de 3 velas donde no se negocio bien, que el precio vuelve a rellenar

Entrada institucional = Barrida de liquidez + Mitigacion de Order Block + FVG + sesgo diario
Es lo que usan ICT, Smart Money Concepts (SMC), bancos.

Esta version aproxima eso con datos diarios de yfinance (sin orderflow real, pero con price action).
"""
import os
import sys
import time
import json
import subprocess
import requests
import pandas as pd
import yfinance as yf

# ── CONFIG ──
def e(key, default=None):
    return os.environ.get(key, default)

TG_TOKEN = e("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT  = e("TELEGRAM_CHAT_ID", "").strip()
TRADE_AUTOMATIC = e("TRADE_AUTOMATIC", "true").lower() in ("1","true","yes")
RUN_INTERVAL_HOURS = int(e("RUN_INTERVAL_HOURS", "2"))  # ahora 2h
MIN_SCORE = int(e("MIN_SCORE", "60"))
MIN_RR    = float(e("MIN_RR", "3.0"))

INSTRUMENTOS = {
    "AUDNZD": {"ticker": "AUDNZD=X",  "deriv_sym": "frxAUDNZD", "stake": float(e("STAKE_AUDNZD", e("STAKE_USD", "10"))), "dec": 5},
    "BTC":    {"ticker": "BTC-USD",   "deriv_sym": "cryBTCUSD", "stake": float(e("STAKE_BTC",    e("STAKE_USD", "10"))), "dec": 1},
    "XAU":    {"ticker": "GC=F",      "deriv_sym": "frxXAUUSD", "stake": float(e("STAKE_XAU",    e("STAKE_USD", "10"))), "dec": 2},
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PLACE_SCRIPT = os.path.join(SCRIPT_DIR, "deriv_place_signal.py")
OPEN_TRADES_FILE = os.path.join(SCRIPT_DIR, "open_trades.json")

# ── TELEGRAM ──
def tg(text):
    if not (TG_TOKEN and TG_CHAT):
        print("[Telegram] Sin token/chat")
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}, timeout=15)
        # print(f"[Telegram] {r.status_code}")
    except Exception as ex:
        print("[Telegram] error:", ex)

# ── INDICADORES BASE ──
def sma(series, n): return series.rolling(n).mean()
def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain/loss
    return 100 - (100/(1+rs))
def atr(df, period=14):
    h,l,c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([(h-l), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()
def macd(series, fast=12, slow=26, signal=9):
    ema_f = series.ewm(span=fast, adjust=False).mean()
    ema_s = series.ewm(span=slow, adjust=False).mean()
    line = ema_f-ema_s
    sig = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line-sig
def swing_extremes(df, window=10, side=2):
    lows, highs = df["Low"], df["High"]
    n = len(df)
    w = min(window, max(3, n-5))
    swing_low = float(lows.iloc[-(w+side):-side].min())
    swing_high = float(highs.iloc[-(w+side):-side].max())
    return swing_low, swing_high
def get_data(ticker):
    try:
        t = yf.Ticker(ticker)
        return t.history(period="8mo", interval="1d")
    except Exception as ex:
        print(f" [datos] {ticker}: {ex}")
        return None

# ── ESTRATEGIA 1: PROFESIONAL (la que ya tenias) ──
def analizar_profesional(ticker):
    df = get_data(ticker)
    if df is None or len(df) < 60: return None
    close = df["Close"]
    last = float(close.iloc[-1])
    s20 = float(sma(close,20).iloc[-1])
    s50 = float(sma(close,50).iloc[-1])
    s200 = float(sma(close,200).iloc[-1]) if len(close)>=200 else float(sma(close,50).iloc[-1])
    r = float(rsi(close).iloc[-1])
    a = float(atr(df).iloc[-1])
    _, _, macd_hist = macd(close)
    hist = float(macd_hist.iloc[-1])
    hist_prev = float(macd_hist.iloc[-2]) if len(macd_hist)>=2 else hist
    swing_low, swing_high = swing_extremes(df)

    razones_bull, bull = [], 0.0
    if last > s20: bull+=2; razones_bull.append("precio > SMA20")
    if last > s50: bull+=2; razones_bull.append("precio > SMA50")
    if last > s200: bull+=2; razones_bull.append("precio > SMA200")
    if s20 > s50: bull+=2; razones_bull.append("SMA20 > SMA50")
    if s50 > s200: bull+=1.5; razones_bull.append("SMA50 > SMA200")
    if hist > 0: bull+=2; razones_bull.append("MACD positivo")
    if hist > hist_prev: bull+=1.5; razones_bull.append("MACD creciendo")
    if 40 <= r <= 65: bull+=1.5; razones_bull.append("RSI en impulso")
    if r < 35: bull+=1.5; razones_bull.append("RSI sobreventa")
    if last > swing_high: bull+=2; razones_bull.append("ruptura de estructura")

    razones_bear, bear = [], 0.0
    if last < s20: bear+=2; razones_bear.append("precio < SMA20")
    if last < s50: bear+=2; razones_bear.append("precio < SMA50")
    if last < s200: bear+=2; razones_bear.append("precio < SMA200")
    if s20 < s50: bear+=2; razones_bear.append("SMA20 < SMA50")
    if s50 < s200: bear+=1.5; razones_bear.append("SMA50 < SMA200")
    if hist < 0: bear+=2; razones_bear.append("MACD negativo")
    if hist < hist_prev: bear+=1.5; razones_bear.append("MACD cayendo")
    if 35 <= r <= 60: bear+=1.5; razones_bear.append("RSI en impulso bajista")
    if r > 65: bear+=1.5; razones_bear.append("RSI sobrecompra")
    if last < swing_low: bear+=2; razones_bear.append("quiebre de estructura")

    diff = bull-bear
    max_possible = 18.0
    confidence = round(min(100.0, abs(diff)/max_possible*100),1)
    direccion = "BUY" if diff>=0 else "SELL"
    razones = (razones_bull if direccion=="BUY" else razones_bear)

    min_sl = 0.6*a; max_sl = 1.5*a
    if direccion=="BUY":
        sl_candidate = swing_low - 0.15*a
        risk = last - sl_candidate
        sl = last - min_sl if risk < min_sl else last - max_sl if risk > max_sl else sl_candidate
        risk = last - sl
    else:
        sl_candidate = swing_high + 0.15*a
        risk = sl_candidate - last
        sl = last + min_sl if risk < min_sl else last + max_sl if risk > max_sl else sl_candidate
        risk = sl - last
    if risk <=0: return None
    tp = last + (risk*MIN_RR) if direccion=="BUY" else last - (risk*MIN_RR)

    return {"precio":last,"s20":s20,"s50":s50,"s200":s200,"rsi":r,"atr":a,"hist":hist,
            "swing_low":swing_low,"swing_high":swing_high,"score":round(diff,1),
            "direccion":direccion,"sl":sl,"tp":tp,"rr":MIN_RR,"confianza":confidence,
            "razones":razones,"estrategia":"PROFESIONAL"}

# ── ESTRATEGIA 2: INSTITUCIONAL / BALLENAS (SMC - Order Block + FVG + Liquidez) ──
def analizar_institucional(ticker):
    """
    Aproximacion SMC con datos diarios:
    - Liquidez: sweep de max/min previos (stop hunt)
    - Order Block: ultima vela contraria antes del impulso
    - FVG: imbalance de 3 velas (low[i] > high[i-2] o high[i] < low[i-2])
    - Sesgo: precio vs high/low 20d y vs open semanal
    """
    df = get_data(ticker)
    if df is None or len(df) < 80: return None
    close = df["Close"]; high = df["High"]; low = df["Low"]; op = df["Open"]
    last = float(close.iloc[-1])
    last_high = float(high.iloc[-1]); last_low = float(low.iloc[-1])
    s20 = float(sma(close,20).iloc[-1])
    a = float(atr(df).iloc[-1])
    swing_low, swing_high = swing_extremes(df, window=15, side=2)

    # Niveles institucionales: max/min 20d = zonas de liquidez
    high_20 = float(high.iloc[-20:-1].max()); low_20 = float(low.iloc[-20:-1].min())
    prev_high = float(high.iloc[-2]); prev_low = float(low.iloc[-2])
    prev_close = float(close.iloc[-2])

    # 1) BARRIDA DE LIQUIDEZ (Liquidity Sweep) - ballenas cazan stops
    # Bullish: low actual < low_20 o < prev_low pero cierra por encima
    sweep_bull = (last_low < low_20*0.999 or last_low < prev_low) and last > last_low + 0.3*(last_high-last_low)
    # Bearish: high actual > high_20 o > prev_high pero cierra por debajo
    sweep_bear = (last_high > high_20*1.001 or last_high > prev_high) and last < last_high - 0.3*(last_high-last_low)

    # 2) FVG / IMBALANCE (3 velas)
    fvg_bull, fvg_bear = False, False
    # Revisa ultimas 5 velas para FVG
    for i in range(-5, -1):
        try:
            # Bullish FVG: low[i] > high[i-2]
            if float(low.iloc[i]) > float(high.iloc[i-2]):
                fvg_bull = True
            if float(high.iloc[i]) < float(low.iloc[i-2]):
                fvg_bear = True
        except: pass

    # 3) ORDER BLOCK (ultima vela contraria antes de impulso)
    ob_bull, ob_bear = False, False
    # OB alcista: ultima vela bajista antes de 2 alcistas
    if len(df)>=5:
        c1, c2, c3 = close.iloc[-3], close.iloc[-2], close.iloc[-1]
        o1, o2, o3 = op.iloc[-3], op.iloc[-2], op.iloc[-1]
        # 2 velas alcistas despues de una bajista
        if (c1 < o1) and (c2 > o2) and (c3 > o3) and (c3 > c1):
            ob_bull = True
        if (c1 > o1) and (c2 < o2) and (c3 < o3) and (c3 < c1):
            ob_bear = True

    # 4) SESGO INSTITUCIONAL (donde estan las ordenes grandes)
    # Precio sobre SMA20 y sobre mid de rango 20d = sesgo alcista institucional
    mid_20 = (high_20+low_20)/2
    bias_bull = last > s20 and last > mid_20
    bias_bear = last < s20 and last < mid_20

    # Score institucional
    razones_bull, bull = [], 0.0
    if sweep_bull: bull+=3; razones_bull.append("🧹 Barrida de liquidez bajista (stop hunt) - ballenas cazaron stops")
    if fvg_bull: bull+=2.5; razones_bull.append("📦 FVG alcista / Imbalance (hueco institucional)")
    if ob_bull: bull+=2.5; razones_bull.append("🏦 Order Block alcista mitigado")
    if bias_bull: bull+=2; razones_bull.append("🏛️ Sesgo institucional alcista (precio > SMA20 y > mid 20d)")
    if last > high_20: bull+=1.5; razones_bull.append("🔓 Ruptura de liquidez externa (high 20d)")
    if prev_close < low_20 and last > low_20: bull+=2; razones_bull.append("🔄 Reclamo de liquidez (false break)")

    razones_bear, bear = [], 0.0
    if sweep_bear: bear+=3; razones_bear.append("🧹 Barrida de liquidez alcista (stop hunt) - ballenas cazaron stops")
    if fvg_bear: bear+=2.5; razones_bear.append("📦 FVG bajista / Imbalance")
    if ob_bear: bear+=2.5; razones_bear.append("🏦 Order Block bajista mitigado")
    if bias_bear: bear+=2; razones_bear.append("🏛️ Sesgo institucional bajista (precio < SMA20 y < mid 20d)")
    if last < low_20: bear+=1.5; razones_bear.append("🔓 Ruptura de liquidez externa (low 20d)")
    if prev_close > high_20 and last < high_20: bear+=2; razones_bear.append("🔄 Rechazo de liquidez (false break)")

    diff = bull-bear
    max_possible = 12.0
    confidence = round(min(100.0, abs(diff)/max_possible*100),1)
    direccion = "BUY" if diff>=0 else "SELL"
    razones = (razones_bull if direccion=="BUY" else razones_bear)

    # SL/TP institucional: SL en el OB o en la liquidez barrida, con ATR
    min_sl = 0.7*a; max_sl = 1.8*a
    if direccion=="BUY":
        # SL bajo el OB o bajo el sweep
        sl_candidate = min(swing_low, low_20) - 0.2*a
        risk = last - sl_candidate
        sl = last - min_sl if risk < min_sl else last - max_sl if risk > max_sl else sl_candidate
        risk = last - sl
    else:
        sl_candidate = max(swing_high, high_20) + 0.2*a
        risk = sl_candidate - last
        sl = last + min_sl if risk < min_sl else last + max_sl if risk > max_sl else sl_candidate
        risk = sl - last
    if risk <=0: return None
    tp = last + (risk*MIN_RR) if direccion=="BUY" else last - (risk*MIN_RR)

    return {"precio":last,"s20":s20,"s50":0,"s200":0,"rsi":50,"atr":a,"hist":0,
            "swing_low":swing_low,"swing_high":swing_high,"score":round(diff,1),
            "direccion":direccion,"sl":sl,"tp":tp,"rr":MIN_RR,"confianza":confidence,
            "razones":razones,"estrategia":"INSTITUCIONAL","high_20":high_20,"low_20":low_20,
            "fvg_bull":fvg_bull,"fvg_bear":fvg_bear,"sweep_bull":sweep_bull,"sweep_bear":sweep_bear}

# ── ELEGIR MEJOR DE 2 ESTRATEGIAS x 3 INSTRUMENTOS = 6 señales ──
def elegir_mejor():
    resultados = []
    for key, cfg in INSTRUMENTOS.items():
        r1 = analizar_profesional(cfg["ticker"])
        if r1:
            r1["nombre"]=f"{key} [PRO]"; r1["base"]=key; r1["deriv_sym"]=cfg["deriv_sym"]; r1["stake"]=cfg["stake"]; r1["dec"]=cfg["dec"]
            resultados.append(r1)
        r2 = analizar_institucional(cfg["ticker"])
        if r2:
            r2["nombre"]=f"{key} [INST]"; r2["base"]=key; r2["deriv_sym"]=cfg["deriv_sym"]; r2["stake"]=cfg["stake"]; r2["dec"]=cfg["dec"]
            resultados.append(r2)

    if not resultados: return None, []
    mejor = max(resultados, key=lambda x: x["confianza"])
    return mejor, resultados

# ── FORMATEO ──
def fmt_px(v, dec): return f"{v:,.{dec}f}"

def texto_senal(mejor, todos, abierto):
    s = mejor
    riesgo = abs(s["precio"]-s["sl"]); benef = abs(s["tp"]-s["precio"]); rr = round(benef/riesgo,2) if riesgo else 0
    lines = [
        f"📊 <b>SEÑAL DOBLE ESTRATEGIA - AUDNZD/BTC/XAU</b>",
        f"🧠 Estrategia: <b>{s['estrategia']}</b> {'(Ballenas/SMC)' if s['estrategia']=='INSTITUCIONAL' else '(Tendencia/Confluencias)'}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🏆 <b>Mejor: {s['nombre']}</b>",
        f"🎯 Dirección: <b>{'COMPRA' if s['direccion']=='BUY' else 'VENTA'}</b>",
        f"📈 Precio: <b>{fmt_px(s['precio'], s['dec'])}</b>",
        f"🛑 SL: {fmt_px(s['sl'], s['dec'])} | 🎯 TP: {fmt_px(s['tp'], s['dec'])}",
        f"📐 R:R = 1:{rr:.2f} | 📊 Confianza: {s['confianza']}/100",
        f"🔧 ATR: {fmt_px(s['atr'], s['dec'])} | Estr: bajo {fmt_px(s['swing_low'], s['dec'])} / alto {fmt_px(s['swing_high'], s['dec'])}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🧠 <b>¿Por qué {s['estrategia']}?</b>",
    ]
    for rz in s["razones"]: lines.append(f"   ✅ {rz}")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("<i>Otras 5 señales evaluadas:</i>")
    for t in sorted(todos, key=lambda x: x["confianza"], reverse=True)[:5]:
        if t["nombre"]==s["nombre"]: continue
        lines.append(f"  • {t['nombre']}: {t['direccion']} conf {t['confianza']} RSI {t.get('rsi',0):.0f}")
    if abierto:
        lines.append(""); lines.append(f"✅ <b>Operación abierta en Deriv DEMO con estrategia {s['estrategia']} - SL/TP nativos.</b>")
    else:
        lines.append(""); lines.append("ℹ️ Modo aviso: no abrí nada.")
    return "\n".join(lines)

def texto_sin_senal(todos):
    lines = ["📊 <b>DOBLE ESTRATEGIA - Sin setup</b>","━━━━━━━━━━━━━━━━━━━━",
             f"🚫 <b>Hoy no hay setup de calidad</b> (ninguno >= {MIN_SCORE}). Disciplina.",
             "<i>Lecturas (6 combinaciones):</i>"]
    for t in sorted(todos, key=lambda x: x["confianza"], reverse=True):
        lines.append(f"  • {t['nombre']}: {t['direccion']} conf {t['confianza']} R:R {t['rr']:.1f} [{t['estrategia']}]")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("ℹ️ Baja MIN_SCORE para más operaciones, pero menos calidad.")
    return "\n".join(lines)

def texto_cierre(trade, profit, is_win):
    # trade es dict guardado de open_trades.json
    s = trade
    emoji = "✅" if is_win else "❌"
    resultado = "GANADA" if is_win else "PERDIDA"
    lines = [
        f"{emoji} <b>OPERACIÓN CERRADA - {resultado}</b>",
        f"🧠 Estrategia: <b>{s.get('estrategia','?')}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📊 {s.get('nombre','?')} {s.get('direccion','?')} | Entrada {fmt_px(s.get('precio',0), s.get('dec',2))}",
        f"🛑 SL: {fmt_px(s.get('sl',0), s.get('dec',2))} | 🎯 TP: {fmt_px(s.get('tp',0), s.get('dec',2))}",
        f"💰 Resultado: <b>{profit:+.2f} USD</b> ({'TP tocado' if is_win else 'SL tocado'})",
        f"🆔 Contract ID: {s.get('contract_id','?')} | Stake {s.get('stake','?')} USD x{ s.get('mult', '?')}",
        f"⏰ Abierta: {s.get('open_time','?')} | Cerrada: {time.strftime('%Y-%m-%d %H:%M')}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📈 Balance demo DOT91988837 actualizado",
    ]
    return "\n".join(lines)

# ── GESTION DE TRADES ABIERTOS ──
def load_open_trades():
    if not os.path.exists(OPEN_TRADES_FILE): return []
    try:
        with open(OPEN_TRADES_FILE, "r") as f: return json.load(f)
    except: return []

def save_open_trades(trades):
    try:
        with open(OPEN_TRADES_FILE, "w") as f: json.dump(trades, f, indent=2)
    except Exception as ex: print(f"[trades] save error {ex}")

def add_open_trade(contract_id, mejor, mult, sl_usd, tp_usd):
    trades = load_open_trades()
    trades.append({
        "contract_id": contract_id,
        "nombre": mejor["nombre"],
        "base": mejor["base"],
        "deriv_sym": mejor["deriv_sym"],
        "direccion": mejor["direccion"],
        "precio": mejor["precio"],
        "sl": mejor["sl"],
        "tp": mejor["tp"],
        "stake": mejor["stake"],
        "dec": mejor["dec"],
        "mult": mult,
        "sl_usd": sl_usd,
        "tp_usd": tp_usd,
        "estrategia": mejor["estrategia"],
        "confianza": mejor["confianza"],
        "open_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    save_open_trades(trades)
    print(f"💾 Trade guardado: {contract_id} {mejor['nombre']} [{mejor['estrategia']}]")

# ── CHEQUEO DE CIERRES VIA DERIV API (nueva) ──
def check_closed_trades():
    trades = load_open_trades()
    if not trades:
        return
    print(f"🔍 Revisando {len(trades)} trades abiertos para cierre...")

    # Usa el mismo flujo PAT+OTP que deriv_place_signal.py pero para consultar
    # Importamos funciones de deriv_place_signal si existe, sino hacemos REST simple
    try:
        import asyncio, urllib.request, urllib.error, json as js
        import websockets

        async def _check():
            # Reusa logica de get_demo_otp_ws
            def rest(meth, path, body=None):
                url = "https://api.derivws.com"+path
                headers = {"Authorization": f"Bearer {os.environ.get('DERIV_API_TOKEN','')}", "Accept":"application/json", "Deriv-App-ID": os.environ.get('DERIV_APP_ID','')}
                data = js.dumps(body).encode() if body else None
                if data: headers["Content-Type"]="application/json"
                req = urllib.request.Request(url, data=data, headers=headers, method=meth)
                try:
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        return resp.getcode(), js.loads(resp.read().decode() or "{}")
                except urllib.error.HTTPError as ex:
                    raw = ex.read().decode(errors="replace")
                    try: return ex.code, js.loads(raw)
                    except: return ex.code, {"raw":raw}
                except Exception as ex: return 0, {"error":str(ex)}

            async def rpc(ws, payload, req_id):
                payload=dict(payload); payload["req_id"]=req_id
                await ws.send(js.dumps(payload))
                deadline=time.time()+20
                while time.time()<deadline:
                    raw=await asyncio.wait_for(ws.recv(), timeout=5)
                    d=js.loads(raw)
                    if d.get("req_id")==req_id: return d
                return {}

            # GET demo account
            code, data = rest("GET","/trading/v1/options/accounts")
            if code!=200: print(f" [check] accounts HTTP {code}"); return trades
            accs = data.get("data") or []
            demo = next((a for a in accs if a.get("account_type")=="demo"), None)
            if not demo: print(" [check] no demo"); return trades
            acc_id = demo.get("account_id")
            code, data = rest("POST", f"/trading/v1/options/accounts/{acc_id}/otp")
            if code!=200: print(f" [check] otp HTTP {code}"); return trades
            ws_url = (data.get("data") or {}).get("url")
            if not ws_url: print(" [check] no ws url"); return trades

            remaining = []
            async with websockets.connect(ws_url, open_timeout=15) as ws:
                for tr in trades:
                    cid = tr.get("contract_id")
                    try:
                        r = await rpc(ws, {"proposal_open_contract":1, "contract_id":cid}, 101)
                        # Si error o contrato no encontrado = ya cerro, revisa statement o asume cerrado
                        if "error" in r:
                            # Puede ser que ya cerro, intenta obtener profit del error o marca como cerrado
                            err = r["error"]
                            # Si dice Contract not found o similar, lo consideramos cerrado sin profit conocido
                            print(f" [check] {cid} error {err.get('code')} - asumo cerrado")
                            # Notifica cierre desconocido
                            tg(texto_cierre(tr, 0.0, False) + "\n⚠️ No se pudo obtener P&L, revisa Deriv")
                            continue
                        poc = r.get("proposal_open_contract") or {}
                        is_sold = poc.get("is_sold") or poc.get("is_expired") or False
                        status = poc.get("status")
                        profit = poc.get("profit")
                        if is_sold or status in ("sold","won","lost"):
                            # Cerrada
                            try: profit_val = float(profit or poc.get("profit") or 0)
                            except: profit_val = 0.0
                            is_win = profit_val > 0
                            print(f" [check] {cid} CERRADA profit {profit_val}")
                            tg(texto_cierre(tr, profit_val, is_win))
                        else:
                            # Sigue abierta
                            remaining.append(tr)
                    except Exception as ex:
                        print(f" [check] {cid} ex {ex}")
                        remaining.append(tr)
            return remaining

        # Corre async
        new_trades = []
        try:
            new_trades = __import__("asyncio").run(_check())
        except Exception as ex:
            print(f"[check] async error {ex}")
            new_trades = trades

        save_open_trades(new_trades)
        if len(new_trades) < len(trades):
            print(f"✅ {len(trades)-len(new_trades)} trades cerrados notificados")
    except Exception as ex:
        print(f"[check_closed] error {ex}")

# ── ABRIR EN DERIV ──
def abrir_en_deriv(mejor):
    env = dict(os.environ)
    env.update({
        "DERIV_SIDE": mejor["direccion"],
        "DERIV_SYMBOL": mejor["deriv_sym"],
        "STAKE_USD": str(mejor["stake"]),
        "SIGNAL_ENTRY": str(mejor["precio"]),
        "SIGNAL_SL": str(mejor["sl"]),
        "SIGNAL_TP": str(mejor["tp"]),
    })
    try:
        p = subprocess.run([sys.executable, PLACE_SCRIPT], capture_output=True, text=True, env=env, timeout=120)
        out = (p.stdout or "") + (p.stderr or "")
        # Intenta extraer contract_id del output
        cid = None
        for line in out.splitlines():
            if "contract_id=" in line:
                try: cid = line.split("contract_id=")[1].split()[0].strip()
                except: pass
        return p.returncode==0, out, cid
    except Exception as ex:
        return False, f"Error Deriv: {ex}", None

# ── CICLO ──
def ciclo():
    print(f"🔍 Analizando 6 señales (3 instrumentos x 2 estrategias) - Intervalo {RUN_INTERVAL_HOURS}h...")
    # 1) Revisa cierres primero
    check_closed_trades()

    mejor, todos = elegir_mejor()
    if mejor is None:
        tg(texto_sin_senal([]))
        return
    if mejor["confianza"] < MIN_SCORE:
        tg(texto_sin_senal(todos))
        print(f" ⏭️ descartado: mejor {mejor['nombre']} conf {mejor['confianza']} < {MIN_SCORE} [{mejor['estrategia']}]")
        return

    abierto=False; cid=None
    if TRADE_AUTOMATIC:
        print(f"🏆 Mejor: {mejor['nombre']} ({mejor['direccion']}) conf {mejor['confianza']} R:R {mejor['rr']:.1f} [{mejor['estrategia']}]")
        tg(f"🚀 <b>Abriendo operación en Deriv DEMO...</b>\n{mejor['nombre']} {'COMPRA' if mejor['direccion']=='BUY' else 'VENTA'}\nEstrategia: <b>{mejor['estrategia']}</b>")
        ok, salida, contract_id = abrir_en_deriv(mejor)
        print(salida[-800:])
        if not ok:
            tg(f"❌ <b>Error al abrir en Deriv:</b>\n<code>{salida[-500:]}</code>")
        else:
            abierto=True
            # Guarda trade para monitoreo de cierre
            try:
                # Extrae mult y sl/tp usd de salida? Usa calculo rapido
                # Para simplificar, parsea de mejor
                # sl_usd/tp_usd ya estan en calc de deriv_place_signal, pero aqui estimamos
                # Vamos a usar stake y mult estimado 20 si BTC
                mult_est = 20 if mejor["base"]=="BTC" else 100
                # Guarda
                if contract_id:
                    add_open_trade(contract_id, mejor, mult_est, 0, 0)
                else:
                    # Si no pudo extraer, guarda con id temporal
                    add_open_trade(f"TEMP-{int(time.time())}", mejor, mult_est, 0, 0)
            except Exception as ex:
                print(f"[save trade] {ex}")

    tg(texto_senal(mejor, todos, abierto))
    print(f"✅ Señal {mejor['nombre']} conf {mejor['confianza']} R:R {mejor['rr']:.1f} [{mejor['estrategia']}] -> {'abierta' if abierto else 'solo aviso'}")

def main():
    if not (TG_TOKEN and TG_CHAT): print("⚠️ Falta TELEGRAM")
    print(f"✅ Bot v3 doble estrategia iniciado. Intervalo {RUN_INTERVAL_HOURS}h | Auto {TRADE_AUTOMATIC} | MIN_SCORE {MIN_SCORE} | MIN_RR {MIN_RR}")
    print(f"   Estrategias: PROFESIONAL (tendencia) + INSTITUCIONAL (ballenas SMC: OB+FVG+Liquidez)")
    while True:
        try: ciclo()
        except Exception as ex:
            print("[ciclo] error:", ex)
            tg(f"❌ <b>Error en análisis:</b> {ex}")
        time.sleep(RUN_INTERVAL_HOURS*3600)

if __name__=="__main__":
    main()
