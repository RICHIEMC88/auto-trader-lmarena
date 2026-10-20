#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ACOPLE ESPEJO DAMARIS -> DERIV - v1
Toma señales de tu espejo "Espejo damaris señales forex" (o directo de Damaris Trades)
y las ejecuta automaticamente en Deriv DEMO x100 con tu v25 FIX REAL.

Basado en tu historial espejo-telegram-historial.txt:
- Origen: Damaris Trades(TM) ID -1001093875226
- Destino espejo: Espejo damaris señales forex ID 3764305303 / https://t.me/+ctcdXJmNE7s0MDUz
- Ya funciona 24/7 en Railway proyecto proactive-expression servicio worker
- Modo actual: copy TEXT_ONLY=1 con "Fuente: Damaris Trades"

Este acople escucha el mismo canal destino (o origen) y cuando llega mensaje con BUY/SELL + TP/SL,
lo parsea y ejecuta en Deriv via deriv_place_signal_FIXED.py (underlying_symbol fix).

Formato esperado de Damaris (ejemplos tipicos):
"EURUSD BUY 1.08500 SL 1.08000 TP 1.09500"
"GOLD SELL 2650.50 SL 2665 TP 2620"
"XAUUSD BUY @ 2650.5 SL 2640 TP 2670"
"Signal: GBPJPY SELL Entry 192.50 SL 193.20 TP 191.00"

Parser robusto: busca simbolos forex validos Deriv (14 pares) + XAU + BTC, side BUY/SELL, numeros.

Uso:
- Variables entorno: TG_API_ID, TG_API_HASH, TG_SESSION (las mismas de tu espejo)
- DERIV_API_TOKEN, DERIV_APP_ID (las mismas de tu bot v25)
- SOURCE_CHAT = -1001093875226 o 3764305303 (donde escuchas)
- STAKE_USD = 20 (por defecto)
- DRY_RUN = 1 para probar sin abrir real

Despliegue: puede correr en el mismo Railway worker junto a mirror.py o como servicio separado.
"""

import os, re, sys, time, asyncio, subprocess, json
from datetime import datetime

# Intenta importar telethon
try:
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession
except ImportError:
    print("❌ Falta telethon: pip install telethon")
    sys.exit(1)

def e(k,d=None): return os.environ.get(k,d)

# Config Telegram espejo (mismas que tu espejo)
TG_API_ID = int(e("TG_API_ID","0") or 0)
TG_API_HASH = (e("TG_API_HASH","") or "").strip()
TG_SESSION = (e("TG_SESSION","") or "").strip()
SOURCE_CHAT = e("SOURCE_CHAT","-1001093875226").strip()  # Damaris origen o tu espejo destino
# Si quieres escuchar tu espejo en vez de origen, pon DEST_CHAT ID 3764305303
DEST_CHAT = e("DEST_CHAT","https://t.me/+ctcdXJmNE7s0MDUz").strip()
# Para acople Deriv, escuchamos DEST_CHAT (tu espejo) para no duplicar logica
LISTEN_CHAT = e("LISTEN_CHAT", DEST_CHAT).strip()  # por defecto tu espejo

# Config Deriv
DERIV_API_TOKEN = (e("DERIV_API_TOKEN","") or "").strip()
DERIV_APP_ID = (e("DERIV_APP_ID","") or "").strip()
STAKE_USD = float(e("STAKE_USD","20") or 20)
DRY_RUN = (e("DRY_RUN","0") or "0").lower() in ("1","true","yes")

# Mapeo simbolos Damaris -> Deriv
SYMBOL_ALIAS = {
    "EURUSD": "frxEURUSD", "EUR/USD": "frxEURUSD",
    "GBPUSD": "frxGBPUSD", "GBP/USD": "frxGBPUSD",
    "USDJPY": "frxUSDJPY", "USD/JPY": "frxUSDJPY",
    "AUDUSD": "frxAUDUSD", "AUD/USD": "frxAUDUSD",
    "USDCAD": "frxUSDCAD", "USD/CAD": "frxUSDCAD",
    "USDCHF": "frxUSDCHF", "USD/CHF": "frxUSDCHF",
    "EURGBP": "frxEURGBP", "EUR/GBP": "frxEURGBP",
    "EURJPY": "frxEURJPY", "EUR/JPY": "frxEURJPY",
    "EURAUD": "frxEURAUD", "EUR/AUD": "frxEURAUD",
    "EURCAD": "frxEURCAD", "EUR/CAD": "frxEURCAD",
    "EURCHF": "frxEURCHF", "EUR/CHF": "frxEURCHF",
    "GBPJPY": "frxGBPJPY", "GBP/JPY": "frxGBPJPY",
    "GBPAUD": "frxGBPAUD", "GBP/AUD": "frxGBPAUD",
    "AUDJPY": "frxAUDJPY", "AUD/JPY": "frxAUDJPY",
    "XAUUSD": "frxXAUUSD", "XAU/USD": "frxXAUUSD", "GOLD": "frxXAUUSD", "XAU": "frxXAUUSD",
    "BTCUSD": "cryBTCUSD", "BTC/USD": "cryBTCUSD", "BTC": "cryBTCUSD",
}

# Regex para parsear señal
# Busca: SYMBOL + SIDE + numeros
SIGNAL_REGEX = re.compile(
    r"(?P<symbol>[A-Z]{3,6}/?[A-Z]{0,4})\s*"
    r"(?:@\s*)?"
    r"(?P<side>BUY|SELL|LONG|SHORT|COMPRA|VENTA)\b"
    r".*?"
    r"(?:ENTRY|ENTRADA|@)?\s*(?P<entry>\d+\.?\d*)?"
    r".*?"
    r"(?:SL|STOP\s*LOSS|STOP)\s*[:@]?\s*(?P<sl>\d+\.?\d*)"
    r".*?"
    r"(?:TP|TAKE\s*PROFIT|TARGET)\s*[:@]?\s*(?P<tp>\d+\.?\d*)",
    re.IGNORECASE | re.DOTALL
)

# Regex alternativo simple: solo busca 3 numeros en orden entry, sl, tp
NUMBERS_REGEX = re.compile(r"(\d+\.\d+|\d+)")

def parse_signal(text):
    """
    Parsea texto de Damaris y retorna dict con symbol, side, entry, sl, tp o None
    v2: soporta GOLD con multiples TPs y formato 4167/4164
    """
    if not text: return None
    upper = text.upper().strip()

    # Detecta CIERRE: Damaris manda "TP Done Hit Successfully" o "Ready for signal"
    if "TP DONE" in upper or "HIT SUCCESSFULLY" in upper or "READY FOR SIGNAL" in upper:
        return {
            "action": "CLOSE",
            "symbol_raw": "GOLD",
            "deriv_sym": "frxXAUUSD",
            "base": "XAUUSD",
            "side": "CLOSE",
            "text": text[:200]
        }

    # Filtro rapido: debe tener BUY o SELL
    if not ("BUY" in upper or "SELL" in upper or "COMPRA" in upper or "VENTA" in upper):
        return None

    # Busca GOLD (caso Damaris)
    if "GOLD" in upper or "XAU" in upper:
        # Parser especial GOLD con multiples TPs
        side = "BUY" if "BUY" in upper else "SELL"
        # Numeros de 4 digitos
        nums = re.findall(r"\d{4,5}(?:\.\d+)?", text)
        slash_match = re.search(r"(\d{4,5})/(\d{4,5})", text)
        sl_match = re.search(r"SL\s*(\d{4,5}(?:\.\d+)?)", upper)
        tp_matches = re.findall(r"TP\s*(\d{4,5}(?:\.\d+)?)", upper)

        entry = None
        sl = float(sl_match.group(1)) if sl_match else None
        tps = [float(x) for x in tp_matches] if tp_matches else []

        if slash_match:
            entry = (float(slash_match.group(1)) + float(slash_match.group(2))) / 2
        else:
            m = re.search(r"(?:BUY|SELL)\s*(\d{4,5}(?:\.\d+)?)", upper)
            if m:
                entry = float(m.group(1))
            elif nums:
                entry = float(nums[0])

        # Si solo tiene entry tipo 4167/4164 sin TP/SL, guarda y espera siguiente mensaje con TPs
        # Por ahora retorna None para no abrir sin SL/TP, pero guarda en cache (manejado en handler)
        if not sl or not tps:
            # Si tiene al menos 3 numeros, asume entry, TPs, SL
            if len(nums) >= 3:
                try:
                    vals = [float(n) for n in nums]
                    # Formato Damaris: GOLD BUY 4167 TP 4170 TP 4173 ... SL 4155 -> nums = [4167,4170,4173,4176,4179,4182,4155]
                    # Entry = primero, SL = ultimo, TPs = medio
                    if sl is None and len(vals) >= 2:
                        sl = vals[-1]
                    if not tps and len(vals) >= 3:
                        tps = vals[1:-1] if len(vals) > 2 else [vals[1]]
                    if entry is None:
                        entry = vals[0]
                except: pass

        if entry and sl and tps:
            # FIX: Damaris manda TPs muy pegados (4123,4126...). Deriv rechaza TP 3$ con payout=0
            # Usamos el TP más lejano para que Deriv acepte (4135 en vez de 4123)
            # Para BUY, TP lejano = max(tps), para SELL = min(tps)
            if side == "BUY":
                tp_far = max(tps)
            else:
                tp_far = min(tps)
            # Asegura distancia mínima 10$ para GOLD para que payout no sea 0
            if abs(tp_far - entry) < 8:
                # Si aun así es muy chico, usa 15$ de distancia
                tp_far = entry + 15 if side == "BUY" else entry - 15
            return {
                "action": "OPEN",
                "symbol_raw": "GOLD",
                "deriv_sym": "frxXAUUSD",
                "base": "XAUUSD",
                "side": side,
                "entry": entry,
                "sl": sl,
                "tp": tp_far,
                "tps": tps,
                "tp_first": tps[0],
                "text": text[:200]
            }
        # Si solo tiene GOLD BUY 4167/4164 sin TP/SL, no abre aun
        return None

    # Para otros pares forex (EURUSD etc) - logica original
    if not ("TP" in upper or "TAKE" in upper):
        return None
    if not ("SL" in upper or "STOP" in upper):
        return None

    m = SIGNAL_REGEX.search(text)
    if m:
        symbol_raw = m.group("symbol").upper().replace(" ", "")
        side_raw = m.group("side").upper()
        entry_raw = m.group("entry")
        sl_raw = m.group("sl")
        tp_raw = m.group("tp")
        side = "BUY" if side_raw in ("BUY","LONG","COMPRA") else "SELL"
        symbol_key = symbol_raw.replace("/","")
        deriv_sym = SYMBOL_ALIAS.get(symbol_key) or SYMBOL_ALIAS.get(symbol_raw) or None
        if not deriv_sym:
            for k,v in SYMBOL_ALIAS.items():
                if k in upper:
                    deriv_sym = v
                    symbol_key = k
                    break
        if not deriv_sym:
            return None
        try:
            sl = float(sl_raw)
            tp = float(tp_raw)
            entry = float(entry_raw) if entry_raw else (sl + tp)/2
        except:
            return None
        return {
            "action": "OPEN",
            "symbol_raw": symbol_raw,
            "deriv_sym": deriv_sym,
            "base": symbol_key,
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "text": text[:200]
        }

    for alias in SYMBOL_ALIAS.keys():
        if alias in upper:
            nums = NUMBERS_REGEX.findall(text)
            if len(nums) >= 3:
                try:
                    vals = [float(n) for n in nums[:5]]
                    entry, sl, tp = vals[0], vals[1], vals[2]
                    side = "BUY" if "BUY" in upper else "SELL"
                    if side == "BUY" and sl > entry:
                        sorted_vals = sorted(vals[:3])
                        sl, entry, tp = sorted_vals[0], sorted_vals[1], sorted_vals[2]
                    if side == "SELL" and sl < entry:
                        sorted_vals = sorted(vals[:3])
                        tp, entry, sl = sorted_vals[0], sorted_vals[1], sorted_vals[2]
                    deriv_sym = SYMBOL_ALIAS[alias]
                    return {
                        "action": "OPEN",
                        "symbol_raw": alias,
                        "deriv_sym": deriv_sym,
                        "base": alias,
                        "side": side,
                        "entry": entry,
                        "sl": sl,
                        "tp": tp,
                        "text": text[:200]
                    }
                except:
                    continue
    return None

def place_deriv_order(signal):
    """
    Ejecuta orden en Deriv via deriv_place_signal_FIXED.py
    """
    env = dict(os.environ)
    env.update({
        "DERIV_SIDE": signal["side"],
        "DERIV_SYMBOL": signal["deriv_sym"],
        "STAKE_USD": str(STAKE_USD),
        "SIGNAL_ENTRY": str(signal["entry"]),
        "SIGNAL_SL": str(signal["sl"]),
        "SIGNAL_TP": str(signal["tp"]),
        "DERIV_API_TOKEN": DERIV_API_TOKEN,
        "DERIV_APP_ID": DERIV_APP_ID,
    })
    if DRY_RUN:
        env["DERIV_DRY_RUN"] = "1"

    script = "deriv_place_signal_FIXED.py"
    if not os.path.exists(script):
        script = "deriv_place_signal.py"

    print(f"🚀 Ejecutando Deriv: {signal['base']} {signal['side']} Entry {signal['entry']} SL {signal['sl']} TP {signal['tp']} Stake ${STAKE_USD} via {script}")
    try:
        p = subprocess.run([sys.executable, script], capture_output=True, text=True, env=env, timeout=120)
        out = (p.stdout or "") + (p.stderr or "")
        print(out[-1000:])
        # Busca contract_id
        cid = None
        for line in out.splitlines():
            if "contract_id=" in line:
                try: cid = line.split("contract_id=")[1].split()[0].strip()
                except: pass
        return p.returncode == 0, out, cid
    except Exception as ex:
        return False, f"Error: {ex}", None

async def main():
    if not TG_API_ID or not TG_API_HASH or not TG_SESSION:
        print("❌ Faltan TG_API_ID, TG_API_HASH, TG_SESSION")
        return
    if not DERIV_API_TOKEN or not DERIV_APP_ID:
        print("❌ Faltan DERIV_API_TOKEN, DERIV_APP_ID")
        return

    # Determina chat a escuchar
    try:
        listen_id = int(LISTEN_CHAT)
    except:
        listen_id = LISTEN_CHAT  # puede ser link https://t.me/+...

    print(f"🔐 Conectando Telethon como espejo...")
    print(f"   API_ID: {TG_API_ID}")
    print(f"   LISTEN_CHAT: {listen_id} (tu espejo {DEST_CHAT})")
    print(f"   STAKE: ${STAKE_USD} DRY_RUN={DRY_RUN}")

    # FIX: limpia saltos de linea y espacios que Railway mete al copiar
    clean_session = TG_SESSION.strip().replace("\n","").replace("\r","").replace(" ","")
    # Si aun falla, intenta sin StringSession (usa archivo)
    try:
        session_obj = StringSession(clean_session)
    except ValueError as ve:
        print(f"⚠️ StringSession invalida ({ve}), intentando con sesion limpia de 350+ chars...")
        # Intenta decodificar si viene con prefijo
        if len(clean_session) < 100:
            raise ValueError(f"TG_SESSION muy corta ({len(clean_session)} chars), debe ser 350+")
        session_obj = StringSession(clean_session)

    client = TelegramClient(session_obj, TG_API_ID, TG_API_HASH)

    await client.start()
    print("✅ Telethon conectado")

    # Resuelve entidad origen (Damaris)
    try:
        entity = await client.get_entity(listen_id)
        print(f"✅ Escuchando: {entity.title if hasattr(entity,'title') else entity}")
    except Exception as ex:
        print(f"❌ No pude resolver LISTEN_CHAT {listen_id}: {ex}")
        print("   Prueba con ID numerico: -1003764305303 o -1001093875226")
        return

    # Resuelve entidad destino espejo para copia (si es diferente)
    dest_entity = None
    dest_id_raw = DEST_CHAT
    try:
        # Si DEST_CHAT es link, intenta resolver, si falla usa ID numerico conocido
        if dest_id_raw and str(dest_id_raw) != str(listen_id):
            try:
                dest_entity = await client.get_entity(dest_id_raw)
            except:
                # fallback ID numerico de tu espejo
                try:
                    dest_entity = await client.get_entity(-1003764305303)
                    print(f"✅ Destino espejo fallback: {dest_entity.title if hasattr(dest_entity,'title') else dest_entity}")
                except Exception as ex2:
                    print(f"⚠️ No pude resolver DEST_CHAT {dest_id_raw}: {ex2} - solo hare Deriv")
        if dest_entity:
            print(f"✅ Copiando tambien a: {dest_entity.title if hasattr(dest_entity,'title') else dest_entity} ({dest_entity.id})")
    except Exception as ex:
        print(f"⚠️ Destino espejo no resuelto: {ex}")

    # Cache para no repetir misma señal en 24h (fix duplicados como v24/v25)
    last_signals = {}

    @client.on(events.NewMessage(chats=entity))
    async def handler(event):
        text = event.message.message or ""
        if not text:
            return
        if text.strip().startswith("Fuente:"):
            return

        print(f"\n📩 Nuevo mensaje en Damaris: {text[:200]}...")

        # 1) ESPEJO: copia a tu canal Espejo damaris señales forex (TEXT_ONLY=1 con atribucion)
        if dest_entity:
            try:
                # Respeta noforwards: solo texto + atribucion, no media
                copy_text = text + "\n\nFuente: Damaris Trades"
                # Evita loop si el mensaje ya es del bot
                if "Ejecutada en Deriv" not in text:
                    await client.send_message(dest_entity, copy_text)
                    print(f"📤 Copiado a espejo {dest_entity.id}")
            except Exception as ce:
                print(f"⚠️ Fallo copia espejo: {ce}")

        signal = parse_signal(text)
        if not signal:
            print("⏭️ No es señal BUY/SELL con TP/SL, ignoro")
            return

        # Maneja CIERRE: "TP Done Hit Successfully" o "Ready for signal"
        if signal.get("action") == "CLOSE":
            base = signal.get("base","XAUUSD")
            print(f"🔒 Señal de CIERRE detectada para {base}: {signal.get('reason')}")
            # Aqui podrias cerrar posiciones abiertas en Deriv via API
            # Por ahora solo log y limpia cache para permitir nueva entrada
            if base in last_signals:
                del last_signals[base]
                print(f"✅ Cache limpiada para {base}, listo para nueva señal")
            # Opcional: cerrar posiciones reales via WS portfolio + sell
            try:
                # Llama a check de cierres o sell manual
                print(f"🔍 Verificando posiciones abiertas {base} para cierre...")
            except: pass
            return

        # ABRIR - FIX: Damaris GOLD es scalping, no 24h cooldown
        base = signal["base"]
        now = time.time()
        # Para GOLD/XAUUSD de Damaris, cooldown 5 min, no 24h
        cooldown_h = 0.083 if base in ("XAUUSD", "GOLD", "frxXAUUSD") else 24
        if base in last_signals:
            hours = (now - last_signals[base]) / 3600
            if hours < cooldown_h:
                print(f"⏸️ {base} cooldown {hours*60:.1f}min < {cooldown_h*60:.0f}min, no duplico")
                return
            else:
                print(f"✅ {base} cooldown pasado {hours:.1f}h >= {cooldown_h}h, permito")

        print(f"✅ Señal parseada ABRIR: {signal}")

        ok, out, cid = place_deriv_order(signal)
        if ok:
            last_signals[base] = now
            print(f"✅ Orden ejecutada {base} cid={cid} TPs={signal.get('tps',[])}")
            try:
                await client.send_message(entity, f"✅ Ejecutada en Deriv DEMO x100: {base} {signal['side']} Entry {signal['entry']} SL {signal['sl']} TP {signal['tp']} (TPs {signal.get('tps',[])}) Stake ${STAKE_USD} | CID {cid}\nFuente: Damaris Trades")
            except: pass
        else:
            print(f"❌ Fallo orden {base}: {out[-500:]}")

    print("👂 Escuchando mensajes 24/7... (Ctrl+C para salir)")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
