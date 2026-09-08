#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
████████████████████████████████████████████████████████████████████████████████
  BOT DE SEÑAL DIARIA — AUDNZD · BITCOIN · ORO
  ████████████████████████████████████████████████████████████████████████████████

  QUÉ HACE (en cada ciclo, por defecto 1 vez al día):
    1) Analiza los 3 instrumentos con indicadores (tendencia + RSI + momentum + ATR).
    2) Elige el que tenga MAYOR CONVICCIÓN según el criterio del sistema.
    3) Te manda la señal por TELEGRAM (siempre, para que veas el análisis).
    4) Si TRADE_AUTOMATIC=true → ABRE la operación en cTrader (cuenta DEMO por defecto).

  CÓMO DETERMINA LA MEJOR OPERACIÓN:
    - Calcula un "score" para cada instrumento (-100 .. +100).
      + positivo = sesgo ALCISTA (compra) | negativo = sesgo BAJISTA (venta).
    - Elige el de MAYOR |score| (más certeza), siempre que supere MIN_SCORE.
    - Calcula SL y TP con el ATR (volatilidad) → riesgo 1.0xATR, objetivo 1.5xATR.

  ⚠️ AVISO HONESTO:
    - El análisis es técnico básico de corto/medio plazo sobre datos de cTrader/Yahoo.
    - NO es asesoría financiera. La decisión y el riesgo son tuyos.
    - Empieza SIEMPRE con CTRADER_ENV=demo y TRADE_AUTOMATIC=false.

  CREDENCIALES (variables de entorno):
    TELEGRAM_BOT_TOKEN   / TELEGRAM_CHAT_ID   (avisos)
    CTRADER_CLIENT_ID    / CTRADER_CLIENT_SECRET / CTRADER_ACCESS_TOKEN /
    CTRADER_ACCOUNT_ID   / CTRADER_ENV (demo|live)

  CONFIG:
    TRADE_AUTOMATIC      true/false   (abrir o solo avisar)
    RUN_INTERVAL_HOURS   horas entre análisis (default 24)
    MIN_SCORE            umbral mínimo de convicción (default 40)
    VOLUME_AUDNZD/VOLUME_BTC/VOLUME_XAU   (lotes)

  EJECUTAR:
    python3 senal_diaria.py
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

TRADE_AUTOMATIC = e("TRADE_AUTOMATIC", "false").lower() in ("1", "true", "yes")
RUN_INTERVAL_HOURS = int(e("RUN_INTERVAL_HOURS", "24"))
MIN_SCORE = int(e("MIN_SCORE", "40"))

# ── Los 3 instrumentos ───────────────────────────────────────────────────────
# ticker      : símbolo para yfinance
# ctrader_sym : nombre del símbolo en cTrader (broker Deriv)
# vol         : lotes por defecto (empezar pequeño en demo)
# pips_dec    : decimales para redondear SL/TP en el broker
INSTRUMENTOS = {
    "AUDNZD": {
        "ticker": "AUDNZD=X",
        "ctrader_sym": "AUDNZD",
        "vol": float(e("VOLUME_AUDNZD", "0.1")),
        "dec": 5,
    },
    "BTC": {
        "ticker": "BTC-USD",
        "ctrader_sym": "BTCUSD",
        "vol": float(e("VOLUME_BTC", "0.01")),
        "dec": 1,
    },
    "XAU": {
        "ticker": "GC=F",           # oro (futuros COMEX se usa como proxy)
        "ctrader_sym": "XAUUSD",
        "vol": float(e("VOLUME_XAU", "0.01")),
        "dec": 2,
    },
}

# Ruta al script que coloca la orden en cTrader (se invoca como subproceso).
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PLACE_SCRIPT = os.path.join(SCRIPT_DIR, "ctrader_place_signal.py")

# ──────────────────────────────────────────────────────────────────────────────
# TELEGRAM
# ──────────────────────────────────────────────────────────────────────────────
def tg(text):
    """Envía un mensaje a Telegram. No falla si falta el token."""
    if not (TG_TOKEN and TG_CHAT):
        print("[Telegram] Sin token/chat configurado. No enviado.")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
    except Exception as ex:
        print("[Telegram] error:", ex)

# ──────────────────────────────────────────────────────────────────────────────
# INDICADORES (RSI, ATR, SMA)
# ──────────────────────────────────────────────────────────────────────────────
def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(window=period).mean()
    loss = (-delta.clip(upper=0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def atr(df, period=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def get_data(ticker):
    """Devuelve velas DIARIAS (últimos ~180 días)."""
    try:
        t = yf.Ticker(ticker)
        df = t.history(period="6mo", interval="1d")
        return df
    except Exception as ex:
        print(f"  [datos] {ticker}: {ex}")
        return None

def analizar(ticker):
    """
    Devuelve dict con la lectura técnica y un 'score' (-100..100).
    Positivo = alcista (compra) · Negativo = bajista (venta).
    """
    df = get_data(ticker)
    if df is None or df.empty or len(df) < 30:
        return None

    close = df["Close"]
    last = float(close.iloc[-1])
    sma20 = float(close.rolling(20).mean().iloc[-1])
    sma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else sma20
    rsi_v = float(rsi(close).iloc[-1])
    atr_v = float(atr(df).iloc[-1])

    # ── Score (pesos razonables) ──
    score = 0.0
    # Tendencia: precio vs media 20
    score += (2 if last > sma20 else -2)
    # Cruce de medias 20/50
    score += (2 if sma20 > sma50 else -2)
    # RSI: desplazamiento desde 50, escalado a ~±4 como máximo en los extremos
    score += max(-4.0, min(4.0, (rsi_v - 50) / 12.0))
    # Momentum de 5 sesiones (pendiente)
    if len(close) >= 6:
        mom = (last / float(close.iloc[-6]) - 1) * 100   # % en 5 días
        score += max(-4.0, min(4.0, mom * 2))
    # Limitamos el rango
    score = max(-100.0, min(100.0, score * 10))

    # SL/TP basados en ATR (volatilidad)
    direccion = "BUY" if score >= 0 else "SELL"
    sl_dist = 1.0 * atr_v
    tp_dist = 1.5 * atr_v
    if direccion == "BUY":
        sl = last - sl_dist
        tp = last + tp_dist
    else:
        sl = last + sl_dist
        tp = last - tp_dist

    return {
        "nombre": None,
        "ticker": ticker,
        "precio": last,
        "sma20": sma20,
        "sma50": sma50,
        "rsi": rsi_v,
        "atr": atr_v,
        "score": round(score, 1),
        "direccion": direccion,
        "sl": sl,
        "tp": tp,
        "confianza": round(abs(score), 1),
    }

# ──────────────────────────────────────────────────────────────────────────────
# SELECCIÓN DE LA MEJOR OPERACIÓN
# ──────────────────────────────────────────────────────────────────────────────
def elegir_mejor():
    resultados = []
    for key, cfg in INSTRUMENTOS.items():
        r = analizar(cfg["ticker"])
        if r is None:
            continue
        r["nombre"] = key
        r["ctrader_sym"] = cfg["ctrader_sym"]
        r["vol"] = cfg["vol"]
        r["dec"] = cfg["dec"]
        resultados.append(r)

    if not resultados:
        return None, None

    # Elegir el de MAYOR confianza (|score|)
    mejor = max(resultados, key=lambda r: r["confianza"])
    return mejor, resultados

# ──────────────────────────────────────────────────────────────────────────────
# FORMATEO PARA TELEGRAM
# ──────────────────────────────────────────────────────────────────────────────
def fmt_px(v, dec):
    return f"{v:,.{dec}f}"

def texto_senal(mejor, todos, abierto):
    s = mejor
    lines = [
        "📊 <b>SEÑAL DIARIA — AUDNZD · BTC · ORO</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🏆 <b>Mejor operación: {s['nombre']}</b>",
        f"🎯 Dirección: <b>{'COMPRA' if s['direccion']=='BUY' else 'VENTA'}</b>",
        f"📈 Precio actual: <b>{fmt_px(s['precio'], s['dec'])}</b>",
        f"📊 Confianza: {s['confianza']}/100",
        f"🛑 SL: {fmt_px(s['sl'], s['dec'])}",
        f"🎯 TP: {fmt_px(s['tp'], s['dec'])}",
        f"📦 Volumen: {s['vol']} lotes",
        f"🔧 ATR: {fmt_px(s['atr'], s['dec'])}",
        "━━━━━━━━━━━━━━━━━━━━",
        "<i>Lecturas de contexto:</i>",
    ]
    for t in todos:
        if t["nombre"] == s["nombre"]:
            continue
        lines.append(
            f"  • {t['nombre']}: {t['direccion']} "
            f"(conf {t['confianza']}) @ {fmt_px(t['precio'], t['dec'])} "
            f"RSI {t['rsi']:.0f}"
        )
    if abierto:
        lines.append("")
        lines.append("✅ <b>Operación enviada a cTrader (DEMO).</b>")
    else:
        lines.append("")
        lines.append("ℹ️ Modo aviso (<b>TRADE_AUTOMATIC=false</b>): no abrí nada.")
    return "\n".join(lines)

def texto_sin_senal(todos):
    lines = ["📊 <b>SEÑAL DIARIA — AUDNZD · BTC · ORO</b>",
             "━━━━━━━━━━━━━━━━━━━━",
             "🚫 <b>Ninguna operación califica hoy</b> (ninguna supera el umbral).",
             "<i>Lecturas:</i>"]
    for t in todos:
        lines.append(f"  • {t['nombre']}: {t['direccion']} (conf {t['confianza']}) "
                     f"RSI {t['rsi']:.0f}")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("ℹ️ Podés bajar MIN_SCORE si querés más operaciones.")
    return "\n".join(lines)

# ──────────────────────────────────────────────────────────────────────────────
# ABRIR EN cTRADER (subproceso) — solo si TRADE_AUTOMATIC
# ──────────────────────────────────────────────────────────────────────────────
def abrir_en_ctrader(mejor):
    env = dict(os.environ)
    env.update({
        "CTRADER_SIDE": mejor["direccion"],
        "CTRADER_SYMBOL": mejor["ctrader_sym"],
        "CTRADER_VOLUME": str(mejor["vol"]),
        "SIGNAL_SL": str(mejor["sl"]),
        "SIGNAL_TP": str(mejor["tp"]),
    })
    try:
        p = subprocess.run([sys.executable, PLACE_SCRIPT],
                           capture_output=True, text=True, env=env, timeout=120)
        out = (p.stdout or "") + (p.stderr or "")
        print("[cTrader]", out)
        return p.returncode == 0, out
    except Exception as ex:
        return False, f"Error al ejecutar cTrader: {ex}"

# ──────────────────────────────────────────────────────────────────────────────
# CICLO PRINCIPAL
# ──────────────────────────────────────────────────────────────────────────────
def ciclo():
    print("🔍 Analizando AUDNZD, BTC y Oro...")
    mejor, todos = elegir_mejor()

    if mejor is None:
        tg(texto_sin_senal([]))
        return

    if mejor["confianza"] < MIN_SCORE:
        tg(texto_sin_senal(todos))
        return

    abierto = False
    if TRADE_AUTOMATIC:
        print(f"🏆 Mejor: {mejor['nombre']} ({mejor['direccion']}) "
              f"confianza {mejor['confianza']}")
        tg("🚀 <b>Abriendo operación en cTrader (DEMO)...</b>\n"
           f"{mejor['nombre']} {'COMPRA' if mejor['direccion']=='BUY' else 'VENTA'}")
        ok, _ = abrir_en_ctrader(mejor)
        abierto = ok

    tg(texto_senal(mejor, todos, abierto))
    print(f"✅ Señal {mejor['nombre']} (conf {mejor['confianza']}) "
          f"-> {'abierta' if abierto else 'solo aviso'}")

def main():
    if not (TG_TOKEN and TG_CHAT):
        print("⚠️  Faltan TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID. Revisa variables de entorno.")
        print("    (igual sigo con la lógica, pero sin avisos)")
    print(f"✅ Bot de señal diaria iniciado. Intervalo: {RUN_INTERVAL_HOURS}h "
          f"| Automático: {TRADE_AUTOMATIC} | MIN_SCORE: {MIN_SCORE}")

    while True:
        try:
            ciclo()
        except Exception as ex:
            print("[ciclo] error:", ex)
            tg(f"❌ <b>Error en el análisis:</b> {ex}")
        time.sleep(RUN_INTERVAL_HOURS * 3600)

if __name__ == "__main__":
    main()
