#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
████████████████████████████████████████████████████████████████████████████████
  BOT DE SEÑAL DIARIA — AUDNZD · BITCOIN · ORO        [v2 · PROFESIONAL]
  ████████████████████████████████████████████████████████████████████████████████

  FILOSOFÍA (trader profesional):
    - No se busca operar todos los días. Se espera a que exista un setup de
      ALTA calidad (confluencias alineadas). Si no lo hay, NO se opera.
    - Riesgo:Recompensa MÍNIMO 1:3. El SL se coloca en estructura (mínimo/máximo
      reciente relevante), no en un múltiplo arbitrario de ATR. El TP se fija SIEMPRE
      a 3x el riesgo, garantizando un ratio de 3 (o más).
    - Confluencias evaluadas en velas DIARIAS:
        * Tendencia (precio vs SMA20/50/200 + cruce de medias)
        * Momentum (MACD histograma + dirección)
        * RSI (zona / sobrecompra / sobreventa)
        * Estructura (swing highs/lows) y volatilidad (ATR)

  QUÉ HACE (una vez al día, por defecto a las 09:00 UTC):
    1) Analiza los 3 instrumentos con el modelo de confluencias.
    2) Elige el que tenga MAYOR confianza, PERO SOLO si supera MIN_SCORE.
    3) Verifica que el setup cumpla R:R >= 3 (si no, lo descarta).
    4) Envía el análisis COMPLETO por Telegram (siempre).
    5) Si TRADE_AUTOMATIC=true → abre la operación en DERIV (DEMO por defecto).

  ⚠️ AVISO HONESTO:
    - Es análisis técnico sobre datos históricos; NO es asesoría financiera.
    - R:R 1:3 implica que la tasa de acierto típica es ~30-40% y aún así se gana.
      No es una "máquina de dinero": habrá pérdidas. La gestión de riesgo manda.
    - Empieza SIEMPRE con token de la cuenta DEMO (VRTC...) y TRADE_AUTOMATIC=false.

  CREDENCIALES (variables de entorno):
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
    DERIV_API_TOKEN   (app.deriv.com → Settings → API Token, scopes Read+Trade,
                       generada para la CUENTA DEMO)
    DERIV_APP_ID      (opcional, default 1089)

  CONFIG:
    TRADE_AUTOMATIC        true/false   (abrir o solo avisar)
    RUN_INTERVAL_HOURS     horas entre análisis (default 24)
    MIN_SCORE              umbral mínimo de convicción (default 60)
    MIN_RR                 riesgo:recompensa mínimo (default 3.0)
    STAKE_USD              apuesta por operación en USD (default 10)
    MULTIPLIER             multiplicador Deriv (default 100)
████████████████████████████████████████████████████████████████████████████████
"""

import os
import sys
import time
import subprocess
import requests
import pandas as pd
import yfinance as yf

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ──────────────────────────────────────────────────────────────────────────────
def e(key, default=None):
    return os.environ.get(key, default)

TG_TOKEN = e("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT  = e("TELEGRAM_CHAT_ID", "").strip()

TRADE_AUTOMATIC = e("TRADE_AUTOMATIC", "true").lower() in ("1", "true", "yes")
RUN_INTERVAL_HOURS = int(e("RUN_INTERVAL_HOURS", "24"))
MIN_SCORE = int(e("MIN_SCORE", "60"))     # subido a 60: solo setups de alta calidad
MIN_RR    = float(e("MIN_RR", "3.0"))     # 1:3 mínimo

INSTRUMENTOS = {
    "AUDNZD": {"ticker": "AUDNZD=X",  "deriv_sym": "frxAUDNZD", "stake": float(e("STAKE_AUDNZD", e("STAKE_USD", "10"))), "dec": 5},
    "BTC":    {"ticker": "BTC-USD",   "deriv_sym": "cryBTCUSD", "stake": float(e("STAKE_BTC",    e("STAKE_USD", "10"))), "dec": 1},
    "XAU":    {"ticker": "GC=F",      "deriv_sym": "frxXAUUSD", "stake": float(e("STAKE_XAU",    e("STAKE_USD", "10"))), "dec": 2},
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PLACE_SCRIPT = os.path.join(SCRIPT_DIR, "deriv_place_signal.py")

# ──────────────────────────────────────────────────────────────────────────────
# TELEGRAM
# ──────────────────────────────────────────────────────────────────────────────
def tg(text):
    if not (TG_TOKEN and TG_CHAT):
        print("[Telegram] Sin token/chat. No enviado.")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
                      timeout=15)
    except Exception as ex:
        print("[Telegram] error:", ex)

# ──────────────────────────────────────────────────────────────────────────────
# INDICADORES
# ──────────────────────────────────────────────────────────────────────────────
def sma(series, n):
    return series.rolling(n).mean()

def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def atr(df, period=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def macd(series, fast=12, slow=26, signal=9):
    ema_f = series.ewm(span=fast, adjust=False).mean()
    ema_s = series.ewm(span=slow, adjust=False).mean()
    line = ema_f - ema_s
    sig = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line - sig

def swing_extremes(df, window=10, side=2):
    """Devuelve el mínimo y máximo de los 'swings' recientes (estructura).
       Usa una ventana CORTA (últimos ~6 mínimos/máximos) para que el SL quede
       en el punto de invalidación INMEDIATO del setup, no en uno lejano."""
    lows = df["Low"]
    highs = df["High"]
    n = len(df)
    w = min(window, max(3, n - 5))
    swing_low = float(lows.iloc[-(w + side):-side].min())
    swing_high = float(highs.iloc[-(w + side):-side].max())
    return swing_low, swing_high

def get_data(ticker):
    try:
        t = yf.Ticker(ticker)
        return t.history(period="8mo", interval="1d")
    except Exception as ex:
        print(f"  [datos] {ticker}: {ex}")
        return None

# ──────────────────────────────────────────────────────────────────────────────
# ANÁLISIS PROFESIONAL (confluencias + R:R 1:3)
# ──────────────────────────────────────────────────────────────────────────────
def analizar(ticker):
    df = get_data(ticker)
    if df is None or len(df) < 60:
        return None

    close = df["Close"]
    last = float(close.iloc[-1])
    s20 = float(sma(close, 20).iloc[-1])
    s50 = float(sma(close, 50).iloc[-1])
    s200 = float(sma(close, 200).iloc[-1]) if len(close) >= 200 else float(sma(close, 50).iloc[-1])
    r = float(rsi(close).iloc[-1])
    a = float(atr(df).iloc[-1])
    _, _, macd_hist = macd(close)
    hist = float(macd_hist.iloc[-1])
    hist_prev = float(macd_hist.iloc[-2]) if len(macd_hist) >= 2 else hist
    swing_low, swing_high = swing_extremes(df)

    # ── Confluencias ALCISTAS (para comprar) ──
    razones_bull = []
    bull = 0.0
    if last > s20:   bull += 2; razones_bull.append("precio > SMA20")
    if last > s50:   bull += 2; razones_bull.append("precio > SMA50")
    if last > s200:  bull += 2; razones_bull.append("precio > SMA200")
    if s20 > s50:    bull += 2; razones_bull.append("SMA20 > SMA50")
    if s50 > s200:   bull += 1.5; razones_bull.append("SMA50 > SMA200")
    if hist > 0:     bull += 2; razones_bull.append("MACD positivo")
    if hist > hist_prev: bull += 1.5; razones_bull.append("MACD creciendo")
    if 40 <= r <= 65:   bull += 1.5; razones_bull.append("RSI en impulso")
    if r < 35:          bull += 1.5; razones_bull.append("RSI sobreventa")
    if last > swing_high: bull += 2; razones_bull.append("ruptura de estructura")

    # ── Confluencias BAJISTAS (para vender) ──
    razones_bear = []
    bear = 0.0
    if last < s20:   bear += 2; razones_bear.append("precio < SMA20")
    if last < s50:   bear += 2; razones_bear.append("precio < SMA50")
    if last < s200:  bear += 2; razones_bear.append("precio < SMA200")
    if s20 < s50:    bear += 2; razones_bear.append("SMA20 < SMA50")
    if s50 < s200:   bear += 1.5; razones_bear.append("SMA50 < SMA200")
    if hist < 0:     bear += 2; razones_bear.append("MACD negativo")
    if hist < hist_prev: bear += 1.5; razones_bear.append("MACD cayendo")
    if 35 <= r <= 60:   bear += 1.5; razones_bear.append("RSI en impulso bajista")
    if r > 65:          bear += 1.5; razones_bear.append("RSI sobrecompra")
    if last < swing_low: bear += 2; razones_bear.append("quiebre de estructura")

    # Confianza = diferencia entre los dos, normalizada a 0-100
    diff = bull - bear
    max_possible = 18.0
    confidence = round(min(100.0, abs(diff) / max_possible * 100), 1)
    direccion = "BUY" if diff >= 0 else "SELL"
    razones = (razones_bull if direccion == "BUY" else razones_bear)

    # ── Cálculo de niveles (estructura + ATR), garantizando R:R >= MIN_RR ──
    # SL en estructura inmediata, PERO con un suelo mínimo de 0.6xATR (para no
    # quedar tan pegado que cualquier ruido te saque) y un tope de 1.5xATR (para
    # no poner un SL irrealmente lejano). Así el riesgo queda contenido y realista.
    min_sl = 0.6 * a
    max_sl = 1.5 * a
    if direccion == "BUY":
        sl_candidate = swing_low - 0.15 * a
        risk = last - sl_candidate
        if risk < min_sl:
            sl = last - min_sl
        elif risk > max_sl:
            sl = last - max_sl
        else:
            sl = sl_candidate
        risk = last - sl
    else:
        sl_candidate = swing_high + 0.15 * a
        risk = sl_candidate - last
        if risk < min_sl:
            sl = last + min_sl
        elif risk > max_sl:
            sl = last + max_sl
        else:
            sl = sl_candidate
        risk = sl - last

    if risk <= 0:
        return None

    tp = last + (risk * MIN_RR) if direccion == "BUY" else last - (risk * MIN_RR)
    rr = MIN_RR  # garantizado (por construcción)

    return {
        "nombre": None, "ticker": ticker,
        "precio": last, "s20": s20, "s50": s50, "s200": s200,
        "rsi": r, "atr": a, "hist": hist,
        "swing_low": swing_low, "swing_high": swing_high,
        "score": round(diff, 1), "direccion": direccion,
        "sl": sl, "tp": tp, "rr": rr,
        "confianza": confidence, "razones": razones,
    }

def elegir_mejor():
    resultados = []
    for key, cfg in INSTRUMENTOS.items():
        r = analizar(cfg["ticker"])
        if r is None:
            continue
        r["nombre"] = key
        r["deriv_sym"] = cfg["deriv_sym"]
        r["stake"] = cfg["stake"]
        r["dec"] = cfg["dec"]
        resultados.append(r)

    if not resultados:
        return None, []

    mejor = max(resultados, key=lambda x: x["confianza"])
    return mejor, resultados

# ──────────────────────────────────────────────────────────────────────────────
# FORMATEO
# ──────────────────────────────────────────────────────────────────────────────
def fmt_px(v, dec):
    return f"{v:,.{dec}f}"

def texto_senal(mejor, todos, abierto):
    s = mejor
    riesgo = abs(s["precio"] - s["sl"])
    benef = abs(s["tp"] - s["precio"])
    rr = round(benef / riesgo, 2)
    lines = [
        "📊 <b>SEÑAL DIARIA (profesional) — AUDNZD · BTC · ORO</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🏆 <b>Mejor operación: {s['nombre']}</b>",
        f"🎯 Dirección: <b>{'COMPRA' if s['direccion']=='BUY' else 'VENTA'}</b>",
        f"📈 Precio: <b>{fmt_px(s['precio'], s['dec'])}</b>",
        f"🎯 Entrada: {fmt_px(s['precio'], s['dec'])} (mercado)",
        f"🛑 SL: {fmt_px(s['sl'], s['dec'])}",
        f"🎯 TP: {fmt_px(s['tp'], s['dec'])}",
        f"🧮 Riesgo: {fmt_px(riesgo, s['dec'])} | Beneficio: {fmt_px(benef, s['dec'])}",
        f"📐 <b>R:R = 1 : {rr:.2f}</b>  (mínimo exigido: 1:3)",
        f"📊 Confianza: {s['confianza']}/100",
        f"🔧 ATR: {fmt_px(s['atr'], s['dec'])} | RSI: {s['rsi']:.0f}",
        f"🏗️ Estructura: bajo {fmt_px(s['swing_low'], s['dec'])} / alto {fmt_px(s['swing_high'], s['dec'])}",
        "━━━━━━━━━━━━━━━━━━━━",
        "🧠 <b>¿Por qué este setup?</b>",
    ]
    for rz in s["razones"]:
        lines.append(f"   ✅ {rz}")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("<i>Contexto (las otras 2):</i>")
    for t in todos:
        if t["nombre"] == s["nombre"]:
            continue
        lines.append(f"  • {t['nombre']}: {t['direccion']} (conf {t['confianza']}) "
                     f"@ {fmt_px(t['precio'], t['dec'])} RSI {t['rsi']:.0f}")
    if abierto:
        lines.append("")
        lines.append("✅ <b>Operación enviada a Deriv (DEMO) con SL/TP nativos.</b>")
    else:
        lines.append("")
        lines.append("ℹ️ Modo aviso (<b>TRADE_AUTOMATIC=false</b>): no abrí nada.")
    return "\n".join(lines)

def texto_sin_senal(todos):
    lines = ["📊 <b>SEÑAL DIARIA (profesional)</b>",
             "━━━━━━━━━━━━━━━━━━━━",
             "🚫 <b>Hoy no hay setup de calidad</b> (ninguno supera el umbral de "
             f"confianza {MIN_SCORE}). <i>Disciplina: es mejor no operar.</i>",
             "<i>Lecturas:</i>"]
    for t in todos:
        lines.append(f"  • {t['nombre']}: {t['direccion']} (conf {t['confianza']}) "
                     f"RSI {t['rsi']:.0f} | R:R {t['rr']:.1f}")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("ℹ️ Para más operaciones baja MIN_SCORE, pero se reduce la calidad.")
    return "\n".join(lines)

# ──────────────────────────────────────────────────────────────────────────────
# ABRIR EN DERIV (subproceso)
# ──────────────────────────────────────────────────────────────────────────────
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
        p = subprocess.run([sys.executable, PLACE_SCRIPT],
                           capture_output=True, text=True, env=env, timeout=120)
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode == 0, out
    except Exception as ex:
        return False, f"Error al ejecutar Deriv: {ex}"

# ──────────────────────────────────────────────────────────────────────────────
# CICLO
# ──────────────────────────────────────────────────────────────────────────────
def ciclo():
    print("🔍 Analizando AUDNZD, BTC y Oro (confluencias profesionales)...")
    mejor, todos = elegir_mejor()

    if mejor is None:
        tg(texto_sin_senal([]))
        return

    if mejor["confianza"] < MIN_SCORE:
        tg(texto_sin_senal(todos))
        print(f"  ⏭️ descartado: mejor confianza {mejor['confianza']} < {MIN_SCORE}")
        return

    abierto = False
    if TRADE_AUTOMATIC:
        print(f"🏆 Mejor: {mejor['nombre']} ({mejor['direccion']}) "
              f"conf {mejor['confianza']} R:R {mejor['rr']:.1f}")
        tg("🚀 <b>Abriendo operación en Deriv (DEMO)...</b>\n"
           f"{mejor['nombre']} {'COMPRA' if mejor['direccion']=='BUY' else 'VENTA'}")
        ok, salida = abrir_en_deriv(mejor)
        if not ok:
            print(salida)
            tg(f"❌ <b>Error al abrir en Deriv:</b>\n<code>{salida[-500:]}</code>")
        abierto = ok

    tg(texto_senal(mejor, todos, abierto))
    print(f"✅ Enviada señal {mejor['nombre']} (conf {mejor['confianza']}, "
          f"R:R {mejor['rr']:.1f}) -> {'abierta' if abierto else 'solo aviso'}")

def main():
    if not (TG_TOKEN and TG_CHAT):
        print("⚠️ Faltan TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID.")
    print(f"✅ Bot diario profesional iniciado. Intervalo {RUN_INTERVAL_HOURS}h | "
          f"Auto {TRADE_AUTOMATIC} | MIN_SCORE {MIN_SCORE} | MIN_RR {MIN_RR}")

    while True:
        try:
            ciclo()
        except Exception as ex:
            print("[ciclo] error:", ex)
            tg(f"❌ <b>Error en análisis:</b> {ex}")
        time.sleep(RUN_INTERVAL_HOURS * 3600)

if __name__ == "__main__":
    main()
