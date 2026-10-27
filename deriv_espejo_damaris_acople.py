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

# FIX: el acople guardaba su mapa en open_trades.json (formato dict) y rompia a forex v25 (espera lista).
# Ahora usa signal_map.json y limpia el open_trades.json danado si es dict.
try:
    if os.path.exists("open_trades.json"):
        _chk = json.loads(open("open_trades.json").read() or "[]")
        if isinstance(_chk, dict):
            open("open_trades.json", "w").write("[]")
            print("[FIX] open_trades.json estaba en formato dict, reiniciado a lista vacia")
except Exception as _ex:
    try: open("open_trades.json", "w").write("[]")
    except Exception: pass

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
# Multi-fuente: Damaris + UK (agrega IDs separados por coma en Railway)
# Ej: LISTEN_CHATS=-1001093875226,-1001149970736
LISTEN_CHATS_RAW = e("LISTEN_CHATS","").strip()
UK_CHAT_ID = e("UK_CHAT_ID","").strip()  # pon ID de UK PROFESSIONAL TRADING SCHOOL

# Config Deriv con diferenciacion por origen
DERIV_API_TOKEN = (e("DERIV_API_TOKEN","") or "").strip()
DERIV_APP_ID = (e("DERIV_APP_ID","") or "").strip()
STAKE_USD = float(e("STAKE_USD","20") or 20)
STAKE_DAMARIS = float(e("STAKE_DAMARIS", str(STAKE_USD)) or STAKE_USD)  # $10 para Damaris
STAKE_UK = float(e("STAKE_UK","12") or 12)  # $12 para UK - asi los diferencias en Deriv
DRY_RUN = (e("DRY_RUN","0") or "0").lower() in ("1","true","yes")

# Fuentes extra (2026-10-26): XAUUSD TRADING CLUB y Sniper Zone (ICT+CRT). Stake distinto para identificarlas en Deriv.
STAKE_XCLUB = float(e("STAKE_XCLUB", "15") or 15)
STAKE_SNIPER = float(e("STAKE_SNIPER", "30") or 30)
EXTRA_SOURCES = {
    "XCLUB": (e("XCLUB_CHAT_ID", "-1001198197283") or "").strip(),
    "SNIPER": (e("SNIPER_CHAT_ID", "-1001466455099") or "").strip(),
}
EXTRA_LABELS = tuple(EXTRA_SOURCES.keys())
BE_SOURCES = ("UK", "XCLUB", "SNIPER")   # fuentes con BE trailing
# BE adaptado a señales con TPs muy pegados (XCLUB/SNIPER): el BE sube 1 TP mas tarde (TP2 en vez de TP1)
# y se deja 1 punto de aire para que el spread/ruido no lo saque en 0.
BE_SHIFT_EXTRA = int(e("BE_SHIFT_EXTRA", "1") or 1)
BE_BUFFER_EXTRA = float(e("BE_BUFFER_EXTRA", "1.0") or 1.0)
def be_params(label):
    if label in EXTRA_LABELS:
        return {"shift": BE_SHIFT_EXTRA, "buffer": BE_BUFFER_EXTRA, "src": label}
    return {"shift": 0, "buffer": 0.0, "src": label}

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

# Handles propios que PUEDEN aparecer dentro de una señal real (firma del canal)
EXTRA_OWN_HANDLES = {"XCLUB": ("@XAUUSDTRADING000",), "SNIPER": ()}
EXTRA_AD_WORDS = ("T.ME/", "HTTP", "JOIN", "VIP", "DISCOUNT", "SUBSCRIPTION", "LIFETIME")

def extra_is_ad(label, text):
    """True si el mensaje de XCLUB/SNIPER es anuncio (otro @, link o palabras de venta VIP)."""
    upper = text.upper()
    own = EXTRA_OWN_HANDLES.get(label, ())
    for h in re.findall(r"@\s?[A-Za-z0-9_]{4,}", upper):
        h = h.replace(" ", "")
        if h not in own:
            return True
    return any(w in upper for w in EXTRA_AD_WORDS)

# TP flexible: "TP 4009", "TP1 4025", "Tp-1 4014", "TP¹ 4009", "TAKE PROFIT 4025"
TP_RE = (r"(?:(?<![A-Z])TP[\u00b9\u00b2\u00b3\u2070-\u2079]?[\s\-_.]*\d?[\s:@\-.]*"
         r"|TAKE\s*PROFIT\s*[:@\-]?\s*)(\d{4,5}(?:\.\d+)?)")

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
        sl_match = re.search(r"(?<![A-Z])(?:SL|STOP\s*LOSS)\s*[:@\-]?\s*(\d{4,5}(?:\.\d+)?)", upper)
        tp_matches = re.findall(TP_RE, upper)

        entry = None
        sl = float(sl_match.group(1)) if sl_match else None
        tps = [float(x) for x in tp_matches] if tp_matches else []

        # Zona de entrada tipo "Entry Zone 4018-4024" o "4002_4012" (solo en el encabezado, antes del primer SL/TP)
        _first_lvl = re.search(r"(?<![A-Z])(?:SL|STOP\s*LOSS|TP|TAKE\s*PROFIT)", upper)
        _header = upper[:_first_lvl.start()] if _first_lvl else upper
        rng_match = re.search(r"(\d{4,5}(?:\.\d+)?)\s*[-_\u2013]\s*(\d{4,5}(?:\.\d+)?)", _header)
        if slash_match:
            entry = (float(slash_match.group(1)) + float(slash_match.group(2))) / 2
        elif rng_match:
            entry = (float(rng_match.group(1)) + float(rng_match.group(2))) / 2
        else:
            m = re.search(r"(?:BUY|SELL)\s*(\d{4,5}(?:\.\d+)?)", upper)
            if m:
                entry = float(m.group(1))
            elif nums:
                entry = float(nums[0])

        # Si solo tiene entry tipo 4167/4164 sin TP/SL, guarda y espera siguiente mensaje con TPs
        # Por ahora retorna None para no abrir sin SL/TP, pero guarda en cache (manejado en handler)
        # FIX 2026-10-26: YA NO se adivina el SL como "ultimo numero". Una señal valida DEBE traer
        # SL explicito (SL / STOP LOSS + numero) y al menos un TP explicito (TP / TAKE PROFIT + numero).
        # Antes un comentario de UK ("BUY ABOVE 3990 targets 4035 & 4055 ... SELL BELOW ... 3950")
        # se leyo como señal real y abrio una operacion falsa.
        if not sl or not tps:
            if entry or nums:
                print(f"⏭️ GOLD sin SL/TP explicitos (SL={sl} TPs={tps}) -> no es señal operable, ignoro")
            return None

        # SANITY: SL del lado correcto y TPs del lado correcto (evita leer mal formatos como UK)
        if entry and sl and tps:
            tps_ok = [t for t in tps if (t > entry if side == "BUY" else t < entry)]
            sl_ok = (sl < entry) if side == "BUY" else (sl > entry)
            if not sl_ok or not tps_ok:
                print(f"⚠️ Señal GOLD descartada por incoherente: {side} entry {entry} SL {sl} TPs {tps}")
                return None
            tps = tps_ok
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

def place_deriv_order(signal, stake_override=None, source_label="DAMARIS"):
    """
    Ejecuta orden en Deriv via deriv_place_signal_FIXED.py
    stake_override y source_label para diferenciar DAMARIS vs UK en Deriv
    """
    stake = stake_override if stake_override else STAKE_USD
    env = dict(os.environ)
    env.update({
        "DERIV_SIDE": signal["side"],
        "DERIV_SYMBOL": signal["deriv_sym"],
        "STAKE_USD": str(stake),
        "SIGNAL_ENTRY": str(signal["entry"]),
        "SIGNAL_SL": str(signal["sl"]),
        "SIGNAL_TP": str(signal["tp"]),
        "DERIV_API_TOKEN": DERIV_API_TOKEN,
        "DERIV_APP_ID": DERIV_APP_ID,
        "SIGNAL_SOURCE": source_label,
    })
    if DRY_RUN:
        env["DERIV_DRY_RUN"] = "1"

    script = "deriv_place_signal_FIXED.py"
    if not os.path.exists(script):
        script = "deriv_place_signal.py"

    print(f"🚀 [{source_label}] Ejecutando Deriv: {signal['base']} {signal['side']} Entry {signal['entry']} SL {signal['sl']} TP {signal['tp']} Stake ${stake} via {script}")
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

    # Construye mapa de chats a escuchar (multi-fuente)
    listen_map = {}
    # 1) LISTEN_CHATS si existe (lista separada por comas)
    if LISTEN_CHATS_RAW:
        for raw in LISTEN_CHATS_RAW.split(","):
            raw=raw.strip()
            if not raw: continue
            try:
                cid = int(raw)
                # detecta origen por ID conocido
                if str(cid) == "-1001093875226":
                    listen_map[cid] = "DAMARIS"
                elif UK_CHAT_ID and str(cid) == UK_CHAT_ID:
                    listen_map[cid] = "UK"
                else:
                    listen_map[cid] = "UNKNOWN"
            except:
                pass
    # 2) UK_CHAT_ID individual
    if UK_CHAT_ID:
        try:
            cid = int(UK_CHAT_ID)
            listen_map[cid] = "UK"
        except: pass
    # 3) fallback LISTEN_CHAT original
    if not listen_map:
        listen_map[listen_id if isinstance(listen_id,int) else -1001093875226] = "DAMARIS"

    # 4) Fuentes extra fijas (XCLUB, SNIPER): no hace falta tocar LISTEN_CHATS en Railway
    for _lbl, _cid in EXTRA_SOURCES.items():
        try:
            if _cid: listen_map[int(_cid)] = _lbl
        except Exception:
            pass

    print(f"🔐 Conectando Telethon como espejo...")
    print(f"   API_ID: {TG_API_ID}")
    print(f"   LISTEN_CHATS: {listen_map} (tu espejo {DEST_CHAT})")
    print(f"   STAKE_DAMARIS=${STAKE_DAMARIS} STAKE_UK=${STAKE_UK} STAKE_XCLUB=${STAKE_XCLUB} STAKE_SNIPER=${STAKE_SNIPER} DRY_RUN={DRY_RUN}")

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

    # Resuelve todas las entidades a escuchar (Damaris + UK)
    entities = []
    entities_map = {}  # id -> source label
    for cid, src in listen_map.items():
        try:
            ent = await client.get_entity(cid)
            entities.append(ent)
            entities_map[ent.id] = src
            print(f"✅ Escuchando [{src}]: {ent.title if hasattr(ent,'title') else ent} ID {ent.id}")
        except Exception as ex:
            print(f"⚠️ No pude resolver {cid} [{src}]: {ex}")
    if not entities:
        # fallback intento con listen_id original
        try:
            ent = await client.get_entity(listen_id)
            entities = [ent]
            entities_map[ent.id] = "DAMARIS"
            print(f"✅ Escuchando fallback: {ent.title if hasattr(ent,'title') else ent}")
        except Exception as ex:
            print(f"❌ No pude resolver LISTEN_CHAT {listen_id}: {ex}")
            return

    # Resuelve entidad destino espejo para copia (si es diferente)
    dest_entity = None
    dest_id_raw = DEST_CHAT
    try:
        if dest_id_raw:
            try:
                dest_entity = await client.get_entity(dest_id_raw)
            except:
                try:
                    dest_entity = await client.get_entity(-1003764305303)
                    print(f"✅ Destino espejo fallback: {dest_entity.title if hasattr(dest_entity,'title') else dest_entity}")
                except Exception as ex2:
                    print(f"⚠️ No pude resolver DEST_CHAT {dest_id_raw}: {ex2} - solo hare Deriv")
        if dest_entity:
            print(f"✅ Copiando tambien a: {dest_entity.title if hasattr(dest_entity,'title') else dest_entity} ({dest_entity.id})")
    except Exception as ex:
        print(f"⚠️ Destino espejo no resuelto: {ex}")

    # Cache para no repetir misma señal en 24h
    last_signals = {}
    # Cache para señales partidas
    pending_gold = {"side": None, "entry": None, "tps": [], "sl": None, "ts": 0, "raw": ""}

    @client.on(events.NewMessage(chats=entities))
    async def handler(event):
        text = event.message.message or ""
        if not text:
            return
        if text.strip().startswith("Fuente:"):
            return

        # Detecta origen real (DAMARIS o UK)
        chat_id = event.chat_id if hasattr(event, 'chat_id') else (event.chat.id if hasattr(event.chat,'id') else 0)
        # Telethon NewMessage con chats multiples usa chat_id
        try:
            src_label = entities_map.get(chat_id, "DAMARIS")
            # si chat_id no esta en map, intenta por peer
            if src_label == "DAMARIS" and len(entities_map)>0:
                # busca si el ID coincide con alguno conocido
                for eid, lbl in entities_map.items():
                    if str(chat_id).endswith(str(eid)[-6:]):
                        src_label = lbl
                        break
        except:
            src_label = "DAMARIS"

        # Stake segun origen
        stake_use = (STAKE_DAMARIS if src_label == "DAMARIS" else STAKE_UK if src_label == "UK"
                     else STAKE_XCLUB if src_label == "XCLUB" else STAKE_SNIPER if src_label == "SNIPER" else STAKE_USD)

        print(f"\n📩 Nuevo mensaje en [{src_label}] {chat_id}: {text[:200]}... | stake ${stake_use}")

        # FILTRO ANUNCIOS: solo deja pasar @Damaris_Trader y señales con numeros reales
        upper = text.upper()
        is_ad = False
        has_at_damaris = "@DAMARIS_TRADER" in upper or "@DAMARIS_TRADER" in text or "@DAMARIS_TRADER" in upper.replace(" ", "")

        # Primero intenta parsear: si es señal valida con SL y TP numericos, NUNCA es anuncio
        # Tambien si es CIERRE (TP Done), NUNCA es anuncio - es importante
        temp_signal = parse_signal(text)
        is_valid_signal = temp_signal and temp_signal.get("action") == "OPEN"
        is_close_signal = temp_signal and temp_signal.get("action") == "CLOSE"

        is_extra = src_label in EXTRA_LABELS
        if is_extra:
            # XCLUB / SNIPER: la señal real es "BUY/SELL + SL + TP" explicitos. XCLUB firma sus señales con su propio @ (permitido);
            # cualquier otro @usuario, link o texto de VIP/join/descuento = anuncio.
            if extra_is_ad(src_label, text):
                is_ad = True
                print(f"🚫 [{src_label}] ANUNCIO (@ajeno/link/VIP) -> no opero ni copio: {text[:80]}...")
            elif not is_valid_signal:
                print(f"⏭️ [{src_label}] mensaje sin señal operable (resultado/charla), ignoro: {text[:60]}...")
                return
            else:
                is_ad = False
        elif is_close_signal:
            is_ad = False  # TP Done Hit Successfully SIEMPRE se copia
            print(f"✅ Señal CIERRE detectada, no es anuncio: {text[:80]}...")
        elif has_at_damaris:
            is_ad = False
            print(f"✅ Contiene @Damaris_Trader, no es anuncio: {text[:80]}...")
        elif is_valid_signal and any(b in upper for b in (
                "T.ME/", "HTTPS://", "HTTP://", "JOIN FAST", "JOIN NOW", "VIP GROUP", "PREMIUM GROUP",
                "OPEN FOR", "LINK EXPIRE", "LINK WILL BE", "CLICK HERE", "REQUEST TO JOIN", "PAID CHANNEL",
                "FIRST 10 MEMBERS", "FREE JOIN", "JOIN HERE")):
            # ANZUELO: parece señal con SL/TP pero trae link/invitacion de grupo VIP = anuncio, NO operar
            is_ad = True
            print(f"🚫 ANUNCIO disfrazado de señal (link/JOIN/VIP) -> no opero ni copio: {text[:80]}...")
        elif is_valid_signal:
            is_ad = False  # señal real, no filtrar aunque contenga "300 PIPS"
        else:
            ad_keywords = [
                "PREMIUM GROUP", "JOIN HERE", "VIP GROUP OPEN", "LIMITED FOR FIRST",
                "YOU KEEP FULL CONTROL", "NO MINIMUM",
                "CLIFFORD TRADING", "PROFIT SHARE", "MINIMUM EQUITY", "CONTACT ADMIN",
                "I WILL DELETE THE LINK", "600+ PIPS TARGET", "200+ PIPS TARGET",
                "FREE !!", "LIFETIME SUBSCRIPTION", "FREE JOIN",
                "1 YEARS FREE", "YEARS FREE JOIN", "REQUEST TO JOIN",
                "OFFER VALID FOR", "CHOSE SIGNALS", "READY FOR VIP SIGNALS",
                "VIP TODAY FREE", "MONTH SUBSCRIPTION", "CHOOSE SIGNALS",
                "PAID CHANNEL", "JOIN FAST", "JOIN NOW", "CLICK HERE", "LINK WILL BE DELETED",
                "FIRST 10 MEMBERS", "IF YOU HAVE 5$", "TRY FAST", "SIGNAL STRATEGY WILL",
            ]
            for kw in ad_keywords:
                if kw in upper:
                    is_ad = True
                    print(f"🚫 Ad keyword '{kw}' detectado")
                    break
            # Links de telegram sin @Damaris_Trader y sin SL/TP numerico = anuncio
            if not is_ad and ("HTTPS://T.ME/" in upper or "T.ME/+" in upper) and not has_at_damaris:
                if not re.search(r"SL\s*\d{3,5}", upper) or not re.search(r"TP\s*\d{3,5}", upper):
                    is_ad = True
                    print(f"🚫 Link t.me sin SL/TP numerico = anuncio")
            # USE BIG LOT sin numeros = anuncio
            if not is_ad and "USE BIG LOT" in upper:
                nums_4d = re.findall(r"\b\d{4,5}(?:\.\d+)?\b", text)
                if len(nums_4d) < 2:
                    is_ad = True
                    print(f"🚫 USE BIG LOT sin numeros = anuncio")
            # Mensajes tipo "BUY okay" sin numeros = no copiar
            if not is_ad and re.match(r"^\s*(BUY|SELL)\s+OKAY", upper):
                is_ad = True
                print(f"🚫 BUY/SELL okay sin numeros = ruido")
            # Mensajes muy cortos sin numeros de 4 digitos y sin SL/TP = anuncio/ruido
            if not is_ad and not is_valid_signal and not is_close_signal:
                if len(re.findall(r"\b\d{3,5}\b", text)) < 2:
                    if "BUY" in upper or "SELL" in upper:
                        if not re.search(r"SL\s*\d", upper) and not re.search(r"TP\s*\d", upper):
                            # Excepcion: si tiene @Damaris_Trader ya lo dejamos pasar arriba
                            is_ad = True
                            print(f"🚫 BUY/SELL sin SL/TP numerico = ruido")

        # 1) ESPEJO: copia SOLO si no es anuncio
        if dest_entity and not is_ad:
            try:
                copy_text = text + f"\n\nFuente: {src_label} Trades"
                if "Ejecutada en Deriv" not in text:
                    await client.send_message(dest_entity, copy_text)
                    print(f"📤 Copiado a espejo {dest_entity.id} [{src_label}]")
            except Exception as ce:
                print(f"⚠️ Fallo copia espejo: {ce}")
        elif is_ad:
            print(f"🚫 Anuncio detectado, no copio a espejo: {text[:100]}...")

        # Anuncio = no opera, no alimenta pendientes
        if is_ad:
            return

        # 2) INTENTO PARSE DIRECTO (todo en 1 mensaje)
        signal = parse_signal(text)

        # 3) SI NO ES SEÑAL COMPLETA, INTENTA JUNTAR MENSAJES PARTIDOS (Damaris manda 3 msgs)
        if not signal:
            upper = text.upper()
            now = time.time()
            # Limpia pending si pasaron 3 min sin completar
            if now - pending_gold["ts"] > 180 and pending_gold["entry"]:
                print(f"🧹 Pending GOLD expirado, limpio")
                pending_gold.update({"side": None, "entry": None, "tps": [], "sl": None, "ts": 0, "raw": ""})

            # Detecta BUY/SELL
            if "BUY" in upper:
                pending_gold["side"] = "BUY"
                pending_gold["ts"] = now
            elif "SELL" in upper:
                pending_gold["side"] = "SELL"
                pending_gold["ts"] = now

            # Detecta entry tipo 4122/4125 o 4122
            m_entry = re.search(r"(?:GOLD|SELL|BUY)\s*(\d{4,5})(?:/(\d{4,5}))?", upper)
            if m_entry:
                e1 = float(m_entry.group(1))
                e2 = float(m_entry.group(2)) if m_entry.group(2) else e1
                pending_gold["entry"] = (e1 + e2) / 2
                pending_gold["ts"] = now
                print(f"📌 Pending entry: {pending_gold['entry']}")

            # Detecta TPs
            tps = re.findall(r"(?:(?<![A-Z])TP\d?|TAKE\s*PROFIT)\s*[:@\-]?\s*(\d{4,5}(?:\.\d+)?)", upper)
            if tps:
                pending_gold["tps"] = [float(x) for x in tps]
                pending_gold["ts"] = now
                print(f"📌 Pending TPs: {pending_gold['tps']}")

            # Detecta SL solo (ej: \"SL 4134\")
            m_sl = re.search(r"(?<![A-Z])(?:SL|STOP\s*LOSS)\s*[:@\-]?\s*(\d{4,5}(?:\.\d+)?)", upper)
            if m_sl:
                pending_gold["sl"] = float(m_sl.group(1))
                pending_gold["ts"] = now
                print(f"📌 Pending SL: {pending_gold['sl']}")

            # Si ya tenemos entry + SL + TPs, armamos señal completa
            if pending_gold["entry"] and pending_gold["sl"] and pending_gold["tps"] and pending_gold["side"]:
                side = pending_gold["side"]
                entry = pending_gold["entry"]
                sl = pending_gold["sl"]
                tps = pending_gold["tps"]
                tp_far = max(tps) if side == "BUY" else min(tps)
                if abs(tp_far - entry) < 8:
                    tp_far = entry + 15 if side == "BUY" else entry - 15
                signal = {
                    "action": "OPEN",
                    "symbol_raw": "GOLD",
                    "deriv_sym": "frxXAUUSD",
                    "base": "XAUUSD",
                    "side": side,
                    "entry": entry,
                    "sl": sl,
                    "tp": tp_far,
                    "tps": tps,
                    "text": f"GOLD {side} {entry} SL {sl} TP {tp_far} (juntado de varios msgs)"
                }
                print(f"✅ Señal COMPLETADA de mensajes partidos: {signal}")
                # Limpia pending para próxima señal
                pending_gold.update({"side": None, "entry": None, "tps": [], "sl": None, "ts": 0, "raw": ""})
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

        # ABRIR - FIX: Damaris GOLD es scalping, no 24h cooldown, por lado
        base = signal["base"]
        side = signal["side"]
        key = f"{base}_{side}_{src_label}"  # diferencia por proveedor y lado
        now = time.time()
        # Para GOLD/XAUUSD de Damaris, cooldown 5 min, no 24h
        cooldown_h = 0.083 if base in ("XAUUSD", "GOLD", "frxXAUUSD") else 24
        if key in last_signals:
            hours = (now - last_signals[key]) / 3600
            if hours < cooldown_h:
                print(f"⏸️ {key} cooldown {hours*60:.1f}min < {cooldown_h*60:.0f}min, no duplico")
                return
            else:
                print(f"✅ {key} cooldown pasado {hours:.1f}h >= {cooldown_h}h, permito")

        print(f"✅ Señal parseada ABRIR [{src_label}]: {signal}")

        ok, out, cid = place_deriv_order(signal, stake_override=stake_use, source_label=src_label)
        if ok:
            last_signals[key] = now
            print(f"✅ Orden ejecutada [{src_label}] {base} {side} cid={cid} TPs={signal.get('tps',[])} stake=${stake_use}")
            # Guarda mapping para diferenciar en Deriv (no spamea canal origen)
            try:
                mapping_file = "signal_map.json"
                data = {}
                if os.path.exists(mapping_file):
                    data = json.loads(open(mapping_file).read() or "{}")
                data[str(cid or f'{base}_{int(now)}')] = {"source": src_label, "base": base, "side": side, "entry": signal['entry'], "sl": signal['sl'], "tp": signal['tp'], "tps": signal.get('tps',[]), "stake": stake_use, "time": datetime.now().isoformat()}
                open(mapping_file, 'w').write(json.dumps(data, indent=2))
            except: pass
            # Si es UK, agrega a trailing BE
            if src_label in BE_SOURCES and cid:
                try:
                    uk_trailing[cid] = {
                        "entry": signal['entry'],
                        "sl": signal['sl'],
                        "tps": signal.get('tps', [signal['tp']]),
                        "side": side,
                        "current_sl_price": signal['sl'],
                        "be_done": False,
                        "tp1_locked": False,
                        "tp3_locked": False,
                        **be_params(src_label),
                    }
                    print(f"📈 [UK BE] Registrado [{src_label}] {cid} para trailing: Entry {signal['entry']} TPs {signal.get('tps',[])}")
                except: pass
            try:
                # Envia confirmacion SOLO a tu Espejo, NO al canal origen (evita ban)
                if dest_entity:
                    be_info = f" + BE {src_label} (TP1->BE, TP3->TP1, TP5->TP3)" if src_label in BE_SOURCES else ""
                    await client.send_message(dest_entity, f"✅ [{src_label}] Ejecutada en Deriv DEMO x100: {base} {side} Entry {signal['entry']} SL {signal['sl']} TP {signal['tp']} (TPs {signal.get('tps',[])}) Stake ${stake_use} | CID {cid}{be_info}\nFuente: {src_label} Trades")
            except: pass
        else:
            print(f"❌ Fallo orden [{src_label}] {base} {side}: {out[-500:]}")
            # Si falla por payout=0, intenta con stake mayor automaticamente
            if "payout=0" in out or "InvalidContractProposal" in out:
                print(f"⚠️ Reintentando [{src_label}] con stake mayor...")
                retry_stake = stake_use * 1.5
                ok2, out2, cid2 = place_deriv_order(signal, stake_override=retry_stake, source_label=src_label)
                if ok2:
                    last_signals[key] = now
                    print(f"✅ Orden ejecutada en reintento [{src_label}] {base} cid={cid2} stake=${retry_stake}")
                    try:
                        if dest_entity:
                            await client.send_message(dest_entity, f"✅ [{src_label}] Reintento OK en Deriv: {base} {side} Stake ${retry_stake} | CID {cid2}")
                        if src_label in BE_SOURCES and cid2:
                            uk_trailing[cid2] = {
                                "entry": signal['entry'],
                                "sl": signal['sl'],
                                "tps": signal.get('tps', [signal['tp']]),
                                "side": side,
                                "current_sl_price": signal['sl'],
                                "be_done": False,
                                "tp1_locked": False,
                                "tp3_locked": False,
                                **be_params(src_label),
                            }
                    except: pass
                else:
                    print(f"❌ Reintento fallo: {out2[-500:]}")

    # BE TRAILING SOLO PARA UK: TP1->BE, TP3->TP1, TP5->TP3  (REAL: opera en Deriv)
    uk_trailing = {}  # contract_id -> {entry, side, tps, current_sl_price, stage, lock}
    UK_TRAIL_FILE = "uk_trailing.json"
    UK_POLL_SEC = float(e("UK_BE_POLL_SEC", "5") or 5)
    BE_BACKUP_LOSS_PCT = float(e("BE_BACKUP_LOSS_PCT", "0.04") or 0.04)  # SL nativo de respaldo al llegar a TP1 (4% del stake)
    # Operacion UK ya abierta antes de este parche (se retira sola cuando Deriv la reporte cerrada)
    UK_SEED = {
        "18429612179": {"entry": 4004.0, "sl": 3994.0, "tps": [4014.0, 4024.0, 4034.0, 4044.0, 4054.0, 4064.0],
                        "side": "BUY", "current_sl_price": 3994.0},
    }
    try:
        if os.path.exists(UK_TRAIL_FILE):
            uk_trailing.update(json.loads(open(UK_TRAIL_FILE).read() or "{}"))
    except Exception as _ex:
        print(f"⚠️ [UK BE] no pude leer {UK_TRAIL_FILE}: {_ex}")
    for _k, _v in UK_SEED.items():
        uk_trailing.setdefault(_k, dict(_v))

    DERIV_REST = "https://api.derivws.com"

    def _deriv_rest(meth, path, body=None, timeout=15):
        import urllib.request, urllib.error
        headers = {"Authorization": f"Bearer {(e('DERIV_API_TOKEN','') or '').strip()}", "Accept": "application/json",
                   "Deriv-App-ID": (e('DERIV_APP_ID', '') or '').strip()}
        data = json.dumps(body).encode() if body else None
        if data: headers["Content-Type"] = "application/json"
        req = urllib.request.Request(DERIV_REST + path, data=data, headers=headers, method=meth)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.getcode(), json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as ex:
            raw = ex.read().decode(errors="replace")
            try: return ex.code, json.loads(raw)
            except Exception: return ex.code, {"raw": raw}
        except Exception as ex:
            return 0, {"error": str(ex)}

    async def _deriv_connect():
        import websockets
        code, data = await asyncio.to_thread(_deriv_rest, "GET", "/trading/v1/options/accounts")
        if code != 200: raise RuntimeError(f"accounts HTTP {code}: {data}")
        accs = data.get("data") or []
        demo = next((a for a in accs if a.get("account_type") == "demo"), None)
        if not demo: raise RuntimeError("sin cuenta demo")
        code, data = await asyncio.to_thread(_deriv_rest, "POST", f"/trading/v1/options/accounts/{demo.get('account_id')}/otp")
        if code != 200: raise RuntimeError(f"otp HTTP {code}: {data}")
        ws_url = (data.get("data") or {}).get("url")
        if not ws_url: raise RuntimeError("otp sin url")
        return await websockets.connect(ws_url, open_timeout=20, close_timeout=5)

    _rid = [1000]
    async def _deriv_rpc(ws, payload, timeout=20):
        _rid[0] += 1
        rid = _rid[0]
        payload = dict(payload); payload["req_id"] = rid
        await ws.send(json.dumps(payload))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            d = json.loads(raw)
            if d.get("req_id") == rid: return d
        return {"error": {"message": "timeout rpc"}}

    def _save_trailing():
        try: open(UK_TRAIL_FILE, "w").write(json.dumps(uk_trailing, indent=2))
        except Exception: pass

    async def _notify(msg):
        try:
            if dest_entity: await client.send_message(dest_entity, msg)
        except Exception: pass

    async def monitor_uk_be():
        """Mueve la proteccion en Deriv: TP1->BE, TP3->TP1, TP5->TP3.
        BE: SL nativo de respaldo (contract_update) + stop virtual (vende a mercado si el precio regresa al nivel).
        TP3/TP5: Deriv no permite SL en ganancia -> stop virtual con venta a mercado."""
        print("📈 Monitor BE UK iniciado (TP1->BE, TP3->TP1, TP5->TP3) - modo REAL Deriv (precio spot de Deriv)")
        ws = None
        fails = {}
        while True:
            try:
                await asyncio.sleep(UK_POLL_SEC)
                if not uk_trailing:
                    if ws is not None:
                        try: await ws.close()
                        except Exception: pass
                        ws = None
                    continue
                if ws is None:
                    ws = await _deriv_connect()
                for cid, info in list(uk_trailing.items()):
                    side = info['side']
                    tps = info.get('tps') or []
                    if not tps: 
                        uk_trailing.pop(cid, None); continue
                    r = await _deriv_rpc(ws, {"proposal_open_contract": 1, "contract_id": int(cid)})
                    if "error" in r:
                        fails[cid] = fails.get(cid, 0) + 1
                        print(f"⚠️ [UK BE] {cid} error poc ({fails[cid]}): {r.get('error')}")
                        if fails[cid] >= 8:
                            print(f"🗑️ [UK BE] {cid} dejo de responder, lo saco del seguimiento")
                            uk_trailing.pop(cid, None); _save_trailing()
                        if "message" in (r.get("error") or {}) and "timeout" in str(r.get("error")):
                            try: await ws.close()
                            except Exception: pass
                            ws = None; break
                        continue
                    fails[cid] = 0
                    poc = r.get("proposal_open_contract") or {}
                    if poc.get("is_sold") or poc.get("is_expired") or poc.get("status") in ("sold", "won", "lost"):
                        print(f"✅ [UK BE] {cid} ya cerrado en Deriv (profit {poc.get('profit')}), fin del seguimiento")
                        uk_trailing.pop(cid, None); _save_trailing()
                        continue
                    try: spot = float(poc.get("current_spot"))
                    except Exception: continue
                    try: entry_spot = float(poc.get("entry_spot") or poc.get("entry_tick") or info['entry'])
                    except Exception: entry_spot = float(info['entry'])
                    try: stake = float(poc.get("buy_price") or 0)
                    except Exception: stake = 0.0

                    tps_sorted = sorted(tps) if side == "BUY" else sorted(tps, reverse=True)
                    hit = 0
                    for i, tp_price in enumerate(tps_sorted):
                        if (side == "BUY" and spot >= tp_price) or (side == "SELL" and spot <= tp_price):
                            hit = i + 1
                    shift = int(info.get('shift', 0))
                    buf = float(info.get('buffer', 0.0))
                    stage = 5 if hit >= 5 + shift else 3 if hit >= 3 + shift else 1 if hit >= 1 + shift else 0
                    if stage > int(info.get('stage', 0)):
                        be_level = (entry_spot - buf) if side == "BUY" else (entry_spot + buf)
                        lock = be_level if stage == 1 else (tps_sorted[0] if stage == 3 else tps_sorted[min(2, len(tps_sorted) - 1)])
                        info['stage'] = stage; info['lock'] = lock; info['current_sl_price'] = lock
                        info['be_done'] = True
                        lbl = {1: "BE (entrada real)", 3: "TP1", 5: "TP3"}[stage]
                        print(f"🔒 [UK BE] [{info.get('src','UK')}] {cid} TP{hit} alcanzado (spot {spot}) -> proteccion a {lbl} {lock}")
                        extra = ""
                        if stage == 1 and stake > 0:
                            loss_amt = round(max(0.5, stake * BE_BACKUP_LOSS_PCT), 2)
                            ru = await _deriv_rpc(ws, {"contract_update": 1, "contract_id": int(cid), "limit_order": {"stop_loss": loss_amt}})
                            if "error" in ru:
                                print(f"⚠️ [UK BE] {cid} contract_update SL respaldo fallo: {ru.get('error')}")
                                extra = " (SL nativo no actualizado, uso stop virtual)"
                            else:
                                print(f"📈 [UK BE] {cid} SL nativo de respaldo -> perdida max ${loss_amt}")
                                extra = f" + SL nativo respaldo -${loss_amt}"
                        _save_trailing()
                        await _notify(f"🔒 [BE {info.get('src','UK')}] {cid} TP{hit} hit (spot {spot}). Proteccion en {lbl} {lock}{extra}")

                    lock = info.get('lock')
                    if lock is not None:
                        crossed = (side == "BUY" and spot <= lock) or (side == "SELL" and spot >= lock)
                        if crossed:
                            print(f"💰 [UK BE] {cid} spot {spot} toco proteccion {lock} -> VENDIENDO a mercado")
                            rs = await _deriv_rpc(ws, {"sell": int(cid), "price": 0})
                            if "error" in rs:
                                print(f"❌ [UK BE] {cid} venta fallo: {rs.get('error')}")
                            else:
                                sold = rs.get("sell") or {}
                                print(f"✅ [UK BE] {cid} vendido: sell_price={sold.get('sold_for') or sold.get('sell_price')}")
                                await _notify(f"✅ [UK BE] {cid} cerrado por proteccion {lock} (spot {spot}). Vendido: {sold.get('sold_for') or sold.get('sell_price')}")
                                uk_trailing.pop(cid, None); _save_trailing()
                _save_trailing()
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                print(f"⚠️ Monitor UK error: {ex}")
                try:
                    if ws is not None: await ws.close()
                except Exception: pass
                ws = None
                await asyncio.sleep(5)

    # Inicia monitor en background
    asyncio.create_task(monitor_uk_be())

    print("👂 Escuchando mensajes 24/7... (Ctrl+C para salir)")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
