#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOT v22 - FIX DUPLICADOS POR RESTART RAILWAY
- v21 abria 3x EURGBP 03:36, 03:45 porque open_trades.json es efimero en Railway y al reiniciar se borra
- v22: verifica posiciones REALES abiertas en Deriv via API, no solo archivo local, para no duplicar mismo par
- Mantiene: 4 pares rentables $20 RR1:1.5 60 max4 + Vol75 1h $10 con Telegram
"""
import os, sys, time, json, subprocess, requests, pandas as pd, yfinance as yf

def e(k,d=None): return os.environ.get(k,d)
TG_TOKEN=e("TELEGRAM_BOT_TOKEN","").strip()
TG_CHAT=e("TELEGRAM_CHAT_ID","").strip()
TRADE_AUTOMATIC=e("TRADE_AUTOMATIC","true").lower() in ("1","true","yes")
RUN_INTERVAL_HOURS=int(e("RUN_INTERVAL_HOURS","2"))
RUN_INTERVAL_VOL75_1H=int(e("RUN_INTERVAL_VOL75_1H","60"))
MIN_RR=float(e("MIN_RR","1.5"))
MAX_OPEN_TRADES=int(e("MAX_OPEN_TRADES","4"))

INSTRUMENTOS={
 "EURCHF": {"ticker":"EURCHF=X", "deriv_sym":"frxEURCHF", "stake":20.0, "dec":5, "interval":"1d", "min_score":60, "period":"1y"},
 "GBPAUD": {"ticker":"GBPAUD=X", "deriv_sym":"frxGBPAUD", "stake":20.0, "dec":5, "interval":"1d", "min_score":60, "period":"1y"},
 "USDCAD": {"ticker":"USDCAD=X", "deriv_sym":"frxUSDCAD", "stake":20.0, "dec":5, "interval":"1d", "min_score":60, "period":"1y"},
 "EURGBP": {"ticker":"EURGBP=X", "deriv_sym":"frxEURGBP", "stake":20.0, "dec":5, "interval":"1d", "min_score":60, "period":"1y"},
}

SCRIPT_DIR=os.path.dirname(os.path.abspath(__file__))
PLACE_MULT_SCRIPT=os.path.join(SCRIPT_DIR,"deriv_place_signal.py")
PLACE_TURBO_SCRIPT=os.path.join(SCRIPT_DIR,"deriv_place_turbo.py")
OPEN_TRADES_FILE=os.path.join(SCRIPT_DIR,"open_trades.json")
OPEN_VOL75_FILE=os.path.join(SCRIPT_DIR,"open_vol75.json")

def tg(text):
    if not (TG_TOKEN and TG_CHAT): return
    try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id":TG_CHAT,"text":text,"parse_mode":"HTML"}, timeout=15)
    except: pass

def sma(s,n): return s.rolling(n).mean()
def rsi(s,p=14):
    d=s.diff(); g=d.clip(lower=0).rolling(p).mean(); l=(-d.clip(upper=0)).rolling(p).mean(); rs=g/l; return 100-(100/(1+rs))
def atr(df,p=14):
    h,l,c=df["High"],df["Low"],df["Close"]; tr=pd.concat([(h-l),(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1); return tr.rolling(p).mean()
def macd(s,f=12,sl=26,sg=9):
    ef=s.ewm(span=f,adjust=False).mean(); es=s.ewm(span=sl,adjust=False).mean(); line=ef-es; sig=line.ewm(span=sg,adjust=False).mean(); return line,sig,line-sig
def swing_extremes(df,w=10,side=2):
    n=len(df); w=min(w,max(3,n-5)); return float(df["Low"].iloc[-(w+side):-side].min()), float(df["High"].iloc[-(w+side):-side].max())
def get_data(ticker, interval="1d", period="1y"):
    try: return yf.Ticker(ticker).history(period=period, interval=interval)
    except: return None

def analizar_profesional(ticker, interval="1d", period="1y"):
    df=get_data(ticker, interval, period)
    if df is None or len(df)<60: return None
    close=df["Close"]; last=float(close.iloc[-1]); s20=float(sma(close,20).iloc[-1]); s50=float(sma(close,50).iloc[-1]); s200=float(sma(close,200).iloc[-1]) if len(close)>=200 else float(sma(close,50).iloc[-1])
    r=float(rsi(close).iloc[-1]); a=float(atr(df).iloc[-1]); _,_,mh=macd(close); hist=float(mh.iloc[-1]); hist_prev=float(mh.iloc[-2]) if len(mh)>=2 else hist
    slw,sh=swing_extremes(df,10,2)
    rb,bu=[],0.0
    if last>s20: bu+=2; rb.append("precio > SMA20")
    if last>s50: bu+=2; rb.append("precio > SMA50")
    if last>s200: bu+=2; rb.append("precio > SMA200")
    if s20>s50: bu+=2; rb.append("SMA20 > SMA50")
    if s50>s200: bu+=1.5; rb.append("SMA50 > SMA200")
    if hist>0: bu+=2; rb.append("MACD positivo")
    if hist>hist_prev: bu+=1.5; rb.append("MACD creciendo")
    if 40<=r<=65: bu+=1.5; rb.append("RSI impulso")
    if r<35: bu+=1.5; rb.append("RSI sobreventa")
    if last>sh: bu+=2; rb.append("ruptura estructura")
    re,be=[],0.0
    if last<s20: be+=2; re.append("precio < SMA20")
    if last<s50: be+=2; re.append("precio < SMA50")
    if last<s200: be+=2; re.append("precio < SMA200")
    if s20<s50: be+=2; re.append("SMA20 < SMA50")
    if s50<s200: be+=1.5; re.append("SMA50 < SMA200")
    if hist<0: be+=2; re.append("MACD negativo")
    if hist<hist_prev: be+=1.5; re.append("MACD cayendo")
    if 35<=r<=60: be+=1.5; re.append("RSI bajista")
    if r>65: be+=1.5; re.append("RSI sobrecompra")
    if last<slw: be+=2; re.append("quiebre estructura")
    diff=bu-be; conf=round(min(100.0,abs(diff)/18.0*100),1); direc="BUY" if diff>=0 else "SELL"; raz=rb if direc=="BUY" else re
    min_sl=0.6*a; max_sl=1.5*a
    if direc=="BUY":
        sl_c=slw-0.15*a; risk=last-sl_c; sl=last-min_sl if risk<min_sl else last-max_sl if risk>max_sl else sl_c; risk=last-sl
    else:
        sl_c=sh+0.15*a; risk=sl_c-last; sl=last+min_sl if risk<min_sl else last+max_sl if risk>max_sl else sl_c; risk=sl-last
    if risk<=0: return None
    tp=last+(risk*MIN_RR) if direc=="BUY" else last-(risk*MIN_RR)
    return {"precio":last,"rsi":r,"atr":a,"swing_low":slw,"swing_high":sh,"direccion":direc,"sl":sl,"tp":tp,"rr":MIN_RR,"confianza":conf,"razones":raz,"estrategia":"PROFESIONAL","interval":interval}

def analizar_institucional(ticker, interval="1d", period="1y"):
    df=get_data(ticker, interval, period)
    if df is None or len(df)<80: return None
    close=df["Close"]; high=df["High"]; low=df["Low"]; op=df["Open"]
    last=float(close.iloc[-1]); last_high=float(high.iloc[-1]); last_low=float(low.iloc[-1]); s20=float(sma(close,20).iloc[-1]); a=float(atr(df).iloc[-1])
    slw,sh=swing_extremes(df,15,2); high_20=float(high.iloc[-20:-1].max()); low_20=float(low.iloc[-20:-1].min()); prev_high=float(high.iloc[-2]); prev_low=float(low.iloc[-2]); prev_close=float(close.iloc[-2])
    sweep_bull=(last_low<low_20*0.999 or last_low<prev_low) and last>last_low+0.3*(last_high-last_low)
    sweep_bear=(last_high>high_20*1.001 or last_high>prev_high) and last<last_high-0.3*(last_high-last_low)
    fvg_bull,fvg_bear=False,False
    for i in range(-5,-1):
        try:
            if float(low.iloc[i])>float(high.iloc[i-2]): fvg_bull=True
            if float(high.iloc[i])<float(low.iloc[i-2]): fvg_bear=True
        except: pass
    ob_bull,ob_bear=False,False
    if len(df)>=5:
        c1,c2,c3=close.iloc[-3],close.iloc[-2],close.iloc[-1]; o1,o2,o3=op.iloc[-3],op.iloc[-2],op.iloc[-1]
        if (c1<o1) and (c2>o2) and (c3>o3) and (c3>c1): ob_bull=True
        if (c1>o1) and (c2<o2) and (c3<o3) and (c3<c1): ob_bear=True
    mid_20=(high_20+low_20)/2; bias_bull=last>s20 and last>mid_20; bias_bear=last<s20 and last<mid_20
    rb,bu=[],0.0
    if sweep_bull: bu+=3; rb.append("🧹 Barrida liquidez bajista")
    if fvg_bull: bu+=2.5; rb.append("📦 FVG alcista")
    if ob_bull: bu+=2.5; rb.append("🏦 Order Block alcista")
    if bias_bull: bu+=2; rb.append("🏛️ Sesgo institucional alcista")
    if last>high_20: bu+=1.5; rb.append("🔓 Ruptura liquidez high 20d")
    if prev_close<low_20 and last>low_20: bu+=2; rb.append("🔄 Reclamo liquidez")
    re,be=[],0.0
    if sweep_bear: be+=3; re.append("🧹 Barrida liquidez alcista")
    if fvg_bear: be+=2.5; re.append("📦 FVG bajista")
    if ob_bear: be+=2.5; re.append("🏦 Order Block bajista")
    if bias_bear: be+=2; re.append("🏛️ Sesgo institucional bajista")
    if last<low_20: be+=1.5; re.append("🔓 Ruptura liquidez low 20d")
    if prev_close>high_20 and last<high_20: be+=2; re.append("🔄 Rechazo liquidez")
    diff=bu-be; conf=round(min(100.0,abs(diff)/12.0*100),1); direc="BUY" if diff>=0 else "SELL"; raz=rb if direc=="BUY" else re
    min_sl=0.7*a; max_sl=1.8*a
    if direc=="BUY":
        sl_c=min(slw,low_20)-0.2*a; risk=last-sl_c; sl=last-min_sl if risk<min_sl else last-max_sl if risk>max_sl else sl_c; risk=last-sl
    else:
        sl_c=max(sh,high_20)+0.2*a; risk=sl_c-last; sl=last+min_sl if risk<min_sl else last+max_sl if risk>max_sl else sl_c; risk=sl-last
    if risk<=0: return None
    tp=last+(risk*MIN_RR) if direc=="BUY" else last-(risk*MIN_RR)
    return {"precio":last,"atr":a,"swing_low":slw,"swing_high":sh,"direccion":direc,"sl":sl,"tp":tp,"rr":MIN_RR,"confianza":conf,"razones":raz,"estrategia":"INSTITUCIONAL","interval":interval}

def analizar_vol75_1h():
    try:
        import asyncio, urllib.request, json as js, websockets, pandas as pd
        async def _get():
            def rest(meth,path,body=None):
                url="https://api.derivws.com"+path
                headers={"Authorization":f"Bearer {os.environ.get('DERIV_API_TOKEN','')}","Accept":"application/json","Deriv-App-ID":os.environ.get('DERIV_APP_ID','')}
                data=js.dumps(body).encode() if body else None
                if data: headers["Content-Type"]="application/json"
                req=urllib.request.Request(url,data=data,headers=headers,method=meth)
                try:
                    with urllib.request.urlopen(req,timeout=15) as resp: return resp.getcode(), js.loads(resp.read().decode() or "{}")
                except Exception as ex: return 0, {"error":str(ex)}
            async def rpc(ws,payload,req_id):
                payload=dict(payload); payload["req_id"]=req_id
                await ws.send(js.dumps(payload))
                deadline=time.time()+10
                while time.time()<deadline:
                    raw=await asyncio.wait_for(ws.recv(),timeout=3)
                    d=js.loads(raw)
                    if d.get("req_id")==req_id: return d
                return {}
            code,data=rest("GET","/trading/v1/options/accounts")
            if code!=200: return None
            accs=data.get("data") or []; demo=next((a for a in accs if a.get("account_type")=="demo"),None)
            if not demo: return None
            acc_id=demo.get("account_id")
            code,data=rest("POST",f"/trading/v1/options/accounts/{acc_id}/otp")
            if code!=200: return None
            ws_url=(data.get("data") or {}).get("url")
            if not ws_url: return None
            async with websockets.connect(ws_url,open_timeout=15) as ws:
                r=await rpc(ws,{"ticks_history":"R_75","adjust_start_time":1,"count":200,"end":"latest","granularity":3600,"style":"candles"},1)
                if "error" in r: return None
                candles=r.get("candles") or []
                if len(candles)<50: return None
                closes=[float(c["close"]) for c in candles]
                df=pd.DataFrame({"Close":closes})
                df["EMA9"]=df["Close"].ewm(span=9,adjust=False).mean()
                df["EMA21"]=df["Close"].ewm(span=21,adjust=False).mean()
                delta=df["Close"].diff()
                gain=delta.clip(lower=0).rolling(14).mean()
                loss=(-delta.clip(upper=0)).rolling(14).mean()
                rs=gain/loss
                df["RSI"]=100-(100/(1+rs))
                df["MACD"]=df["Close"].ewm(span=12,adjust=False).mean()-df["Close"].ewm(span=26,adjust=False).mean()
                df["MACD_SIG"]=df["MACD"].ewm(span=9,adjust=False).mean()
                last=df.iloc[-1]; prev=df.iloc[-2]
                ema9=float(last["EMA9"]); ema21=float(last["EMA21"]); rsi_val=float(last["RSI"]); macd_val=float(last["MACD"]); macd_sig=float(last["MACD_SIG"])
                price=float(last["Close"])
                bull=0; bear=0; razones=[]
                if ema9>ema21: bull+=2; razones.append("EMA9 > EMA21 alcista")
                else: bear+=2; razones.append("EMA9 < EMA21 bajista")
                if 50<rsi_val<70: bull+=1.5; razones.append(f"RSI {rsi_val:.1f} impulso alcista")
                elif 30<rsi_val<50: bear+=1.5; razones.append(f"RSI {rsi_val:.1f} impulso bajista")
                if macd_val>macd_sig: bull+=1.5; razones.append("MACD alcista")
                else: bear+=1.5; razones.append("MACD bajista")
                if float(prev["EMA9"])<=float(prev["EMA21"]) and ema9>ema21: bull+=2; razones.append("🔀 Cruce alcista")
                if float(prev["EMA9"])>=float(prev["EMA21"]) and ema9<ema21: bear+=2; razones.append("🔀 Cruce bajista")
                if rsi_val<30: bull+=1; razones.append("RSI sobreventa rebote")
                if rsi_val>70: bear+=1; razones.append("RSI sobrecompra caida")
                diff=bull-bear
                conf=round(min(100.0,abs(diff)/6.0*100),1)
                if conf<70: return None
                direccion="CALL" if diff>0 else "PUT"
                return {"direccion":direccion,"confianza":conf,"razones":razones,"precio":price,"rsi":rsi_val,"atr":0,"sl":0,"tp":0,"rr":1,"estrategia":"VOL75_1H","interval":"1h","nombre":"R_75 [VOL75 1h]","base":"R_75","deriv_sym":"R_75","stake":10.0,"dec":2,"min_score":70,"duration":1,"duration_unit":"h"}
        try: return __import__("asyncio").run(_get())
        except: return None
    except: return None

def elegir_mejor():
    res=[]
    for key,cfg in INSTRUMENTOS.items():
        r1=analizar_profesional(cfg["ticker"], cfg["interval"], cfg["period"])
        if r1: r1["nombre"]=f"{key} [PRO]"; r1["base"]=key; r1["deriv_sym"]=cfg["deriv_sym"]; r1["stake"]=cfg["stake"]; r1["dec"]=cfg["dec"]; r1["min_score"]=cfg["min_score"]; r1["interval"]=cfg["interval"]; res.append(r1)
        r2=analizar_institucional(cfg["ticker"], cfg["interval"], cfg["period"])
        if r2: r2["nombre"]=f"{key} [INST]"; r2["base"]=key; r2["deriv_sym"]=cfg["deriv_sym"]; r2["stake"]=cfg["stake"]; r2["dec"]=cfg["dec"]; r2["min_score"]=cfg["min_score"]; r2["interval"]=cfg["interval"]; res.append(r2)
    if not res: return None,[]
    validos=[r for r in res if r["confianza"]>=r["min_score"]]
    if not validos:
        mejor=max(res,key=lambda x:x["confianza"])
        return mejor, res
    validos_sorted=sorted(validos,key=lambda x:x["confianza"], reverse=True)
    return validos_sorted[0], validos_sorted

def fmt_px(v,d): return f"{v:,.{d}f}"
def texto_senal(mejor):
    s=mejor
    if s["estrategia"]=="VOL75_1H":
        return "\n".join([
            f"⚡ <b>VOL75 1h ABIERTA - {s['nombre']}</b>",f"🧠 {s['estrategia']} | {s['interval']} | Conf {s['confianza']}/{s['min_score']}",
            "━━━━━━━━━━━━━━━━━━━━",f"🎯 {'RISE' if s['direccion']=='CALL' else 'FALL'} @ {fmt_px(s['precio'], s['dec'])} | RSI {s['rsi']:.1f}",
            f"⏱️ Exp 1h | Stake ${s['stake']}",
            "━━━━━━━━━━━━━━━━━━━━"] + [f"✅ {rz}" for rz in s["razones"]] + [f"✅ Orden VOL75 DEMO"])
    riesgo=abs(s["precio"]-s["sl"]); benef=abs(s["tp"]-s["precio"]); rr=round(benef/riesgo,2) if riesgo else 0
    return "\n".join([
        f"🚀 <b>SEÑAL ABIERTA - {s['nombre']}</b>",f"🧠 {s['estrategia']} | {s['interval']} | Conf {s['confianza']}/{s['min_score']} | R:R 1:{MIN_RR} Stake ${s['stake']}",
        "━━━━━━━━━━━━━━━━━━━━",f"🎯 {'COMPRA' if s['direccion']=='BUY' else 'VENTA'} @ {fmt_px(s['precio'], s['dec'])}",f"🛑 SL {fmt_px(s['sl'], s['dec'])} | 🎯 TP {fmt_px(s['tp'], s['dec'])}",f"📐 R:R 1:{rr:.2f}",
        "━━━━━━━━━━━━━━━━━━━━"] + [f"✅ {rz}" for rz in s["razones"]] + [f"✅ Orden Deriv DEMO x100 | 4 pares $20"])

def texto_cierre(trade, profit, is_win, raw_poc):
    s=trade; emoji="✅" if is_win else "❌"; res="GANADA" if is_win else "PERDIDA"
    profit_txt = f"{profit:+.2f} USD"
    if profit==0 and not is_win:
        buy_price = raw_poc.get("buy_price") or s.get("stake",20)
        try: profit_txt = f"{-float(buy_price):+.2f} USD (est. SL)"
        except: profit_txt = f"{profit:+.2f} USD (SL)"
    return "\n".join([
        f"{emoji} <b>CERRADA - {res}</b> | 🧠 {s.get('estrategia','?')} | {s.get('interval','?')}","━━━━━━━━━━━━━━━━━━━━",
        f"📊 {s.get('nombre','?')} {s.get('direccion','?')} @ {fmt_px(s.get('precio',0), s.get('dec',2))}",
        f"💰 {profit_txt} | 🆔 {s.get('contract_id','?')} x{s.get('mult','?')}",
        f"⏰ {s.get('open_time','?')} → {time.strftime('%Y-%m-%d %H:%M')}",
    ])

def load_open_trades():
    if not os.path.exists(OPEN_TRADES_FILE): return []
    try:
        with open(OPEN_TRADES_FILE,"r") as f: return json.load(f)
    except: return []
def save_open_trades(tr):
    try:
        with open(OPEN_TRADES_FILE,"w") as f: json.dump(tr,f,indent=2)
    except: pass
def add_open_trade(cid,mejor,mult,sl_usd,tp_usd):
    tr=load_open_trades()
    tr.append({"contract_id":cid,"nombre":mejor["nombre"],"base":mejor["base"],"deriv_sym":mejor["deriv_sym"],"direccion":mejor["direccion"],"precio":mejor["precio"],"sl":mejor["sl"],"tp":mejor["tp"],"stake":mejor["stake"],"dec":mejor["dec"],"mult":mult,"estrategia":mejor["estrategia"],"confianza":mejor["confianza"],"interval":mejor["interval"],"min_score":mejor["min_score"],"open_time":time.strftime("%Y-%m-%d %H:%M:%S")})
    save_open_trades(tr)
def load_open_vol75():
    if not os.path.exists(OPEN_VOL75_FILE): return []
    try:
        with open(OPEN_VOL75_FILE,"r") as f: return json.load(f)
    except: return []
def save_open_vol75(tr):
    try:
        with open(OPEN_VOL75_FILE,"w") as f: json.dump(tr,f,indent=2)
    except: pass
def add_open_vol75(cid,mejor):
    tr=load_open_vol75()
    tr.append({"contract_id":cid,"nombre":mejor["nombre"],"base":mejor["base"],"deriv_sym":mejor["deriv_sym"],"direccion":mejor["direccion"],"precio":mejor["precio"],"stake":mejor["stake"],"dec":mejor["dec"],"estrategia":mejor["estrategia"],"confianza":mejor["confianza"],"interval":mejor["interval"],"min_score":mejor["min_score"],"open_time":time.strftime("%Y-%m-%d %H:%M:%S")})
    save_open_vol75(tr)

# v22 FIX: verifica posiciones REALES en Deriv, no solo archivo local
def get_open_symbols_from_deriv():
    """Obtiene simbolos realmente abiertos en Deriv para no duplicar tras restart"""
    try:
        import asyncio, urllib.request, urllib.error, json as js, websockets
        async def _check():
            def rest(meth,path,body=None):
                url="https://api.derivws.com"+path
                headers={"Authorization":f"Bearer {os.environ.get('DERIV_API_TOKEN','')}","Accept":"application/json","Deriv-App-ID":os.environ.get('DERIV_APP_ID','')}
                data=js.dumps(body).encode() if body else None
                if data: headers["Content-Type"]="application/json"
                req=urllib.request.Request(url,data=data,headers=headers,method=meth)
                try:
                    with urllib.request.urlopen(req,timeout=15) as resp: return resp.getcode(), js.loads(resp.read().decode() or "{}")
                except Exception as ex: return 0, {"error":str(ex)}
            async def rpc(ws,payload,req_id):
                payload=dict(payload); payload["req_id"]=req_id
                await ws.send(js.dumps(payload))
                deadline=time.time()+15
                while time.time()<deadline:
                    raw=await asyncio.wait_for(ws.recv(),timeout=3)
                    d=js.loads(raw)
                    if d.get("req_id")==req_id: return d
                return {}
            code,data=rest("GET","/trading/v1/options/accounts")
            if code!=200: return []
            accs=data.get("data") or []; demo=next((a for a in accs if a.get("account_type")=="demo"),None)
            if not demo: return []
            acc_id=demo.get("account_id")
            code,data=rest("POST",f"/trading/v1/options/accounts/{acc_id}/otp")
            if code!=200: return []
            ws_url=(data.get("data") or {}).get("url")
            if not ws_url: return []
            symbols=[]
            async with websockets.connect(ws_url,open_timeout=15) as ws:
                # pide portfolio
                r=await rpc(ws,{"portfolio":1},1)
                portfolio=r.get("portfolio") or {}
                contracts=portfolio.get("contracts") or []
                for c in contracts:
                    sym=c.get("symbol") or c.get("underlying_symbol") or ""
                    # mapea frxEURGBP -> EURGBP
                    sym=sym.replace("frx","").replace("R_75","R_75")
                    if "EURGBP" in sym: symbols.append("EURGBP")
                    elif "EURCHF" in sym: symbols.append("EURCHF")
                    elif "GBPAUD" in sym: symbols.append("GBPAUD")
                    elif "USDCAD" in sym: symbols.append("USDCAD")
                    elif "R_75" in sym: symbols.append("R_75")
            return symbols
        try:
            return __import__("asyncio").run(_check())
        except:
            return []
    except:
        return []

def is_symbol_open(base):
    # v22: verifica archivo local + Deriv real
    trades=load_open_trades()
    for t in trades:
        if t.get("base")==base:
            return True
    # verifica en Deriv real
    real_symbols=get_open_symbols_from_deriv()
    if base in real_symbols:
        print(f" 🔒 {base} ya abierto en Deriv real (aunque json vacio), no duplico")
        return True
    return False

def check_closed_trades():
    trades=load_open_trades()
    if not trades:
        # v22: si json vacio pero hay posiciones reales, reconstruye json vacio? No, deja que portfolio las cierre
        # Pero intenta sincronizar: si hay posiciones reales, no borra
        return
    try:
        import asyncio, urllib.request, urllib.error, json as js, websockets
        async def _check():
            def rest(meth,path,body=None):
                url="https://api.derivws.com"+path
                headers={"Authorization":f"Bearer {os.environ.get('DERIV_API_TOKEN','')}","Accept":"application/json","Deriv-App-ID":os.environ.get('DERIV_APP_ID','')}
                data=js.dumps(body).encode() if body else None
                if data: headers["Content-Type"]="application/json"
                req=urllib.request.Request(url,data=data,headers=headers,method=meth)
                try:
                    with urllib.request.urlopen(req,timeout=15) as resp: return resp.getcode(), js.loads(resp.read().decode() or "{}")
                except urllib.error.HTTPError as ex:
                    raw=ex.read().decode(errors="replace")
                    try: return ex.code, js.loads(raw)
                    except: return ex.code, {"raw":raw}
                except Exception as ex: return 0, {"error":str(ex)}
            async def rpc(ws,payload,req_id):
                payload=dict(payload); payload["req_id"]=req_id
                await ws.send(js.dumps(payload))
                deadline=time.time()+20
                while time.time()<deadline:
                    raw=await asyncio.wait_for(ws.recv(),timeout=5)
                    d=js.loads(raw)
                    if d.get("req_id")==req_id: return d
                return {}
            code,data=rest("GET","/trading/v1/options/accounts")
            if code!=200: return trades
            accs=data.get("data") or []; demo=next((a for a in accs if a.get("account_type")=="demo"),None)
            if not demo: return trades
            acc_id=demo.get("account_id")
            code,data=rest("POST",f"/trading/v1/options/accounts/{acc_id}/otp")
            if code!=200: return trades
            ws_url=(data.get("data") or {}).get("url")
            if not ws_url: return trades
            remaining=[]
            async with websockets.connect(ws_url,open_timeout=15) as ws:
                for tr in trades:
                    cid=tr.get("contract_id")
                    if not cid or cid.startswith("TEMP"): 
                        remaining.append(tr)
                        continue
                    try:
                        r=await rpc(ws,{"proposal_open_contract":1,"contract_id":cid},101)
                        if "error" in r:
                            remaining.append(tr)
                            continue
                        poc=r.get("proposal_open_contract") or {}
                        is_sold=poc.get("is_sold") or poc.get("is_expired") or False
                        status=poc.get("status")
                        profit = poc.get("profit")
                        if profit is None: profit = poc.get("pnl")
                        if profit is None:
                            try:
                                sell = float(poc.get("sell_price") or 0)
                                buy = float(poc.get("buy_price") or tr.get("stake",20))
                                if sell>0: profit = sell - buy
                            except: profit = 0
                        try: profit_val=float(profit or 0)
                        except: profit_val=0.0
                        if is_sold or status in ("sold","won","lost"):
                            tg(texto_cierre(tr,profit_val,profit_val>0,poc))
                        else:
                            remaining.append(tr)
                    except: remaining.append(tr)
            return remaining
        new_trades=[]
        try: new_trades=__import__("asyncio").run(_check())
        except: new_trades=trades
        save_open_trades(new_trades)
    except: pass

def check_closed_vol75():
    trades=load_open_vol75()
    if not trades: return
    try:
        import asyncio, urllib.request, urllib.error, json as js, websockets
        async def _check():
            def rest(meth,path,body=None):
                url="https://api.derivws.com"+path
                headers={"Authorization":f"Bearer {os.environ.get('DERIV_API_TOKEN','')}","Accept":"application/json","Deriv-App-ID":os.environ.get('DERIV_APP_ID','')}
                data=js.dumps(body).encode() if body else None
                if data: headers["Content-Type"]="application/json"
                req=urllib.request.Request(url,data=data,headers=headers,method=meth)
                try:
                    with urllib.request.urlopen(req,timeout=15) as resp: return resp.getcode(), js.loads(resp.read().decode() or "{}")
                except Exception as ex: return 0, {"error":str(ex)}
            async def rpc(ws,payload,req_id):
                payload=dict(payload); payload["req_id"]=req_id
                await ws.send(js.dumps(payload))
                deadline=time.time()+20
                while time.time()<deadline:
                    raw=await asyncio.wait_for(ws.recv(),timeout=5)
                    d=js.loads(raw)
                    if d.get("req_id")==req_id: return d
                return {}
            code,data=rest("GET","/trading/v1/options/accounts")
            if code!=200: return trades
            accs=data.get("data") or []; demo=next((a for a in accs if a.get("account_type")=="demo"),None)
            if not demo: return trades
            acc_id=demo.get("account_id")
            code,data=rest("POST",f"/trading/v1/options/accounts/{acc_id}/otp")
            if code!=200: return trades
            ws_url=(data.get("data") or {}).get("url")
            if not ws_url: return trades
            remaining=[]
            async with websockets.connect(ws_url,open_timeout=15) as ws:
                for tr in trades:
                    cid=tr.get("contract_id")
                    if not cid or cid.startswith("TEMP"): 
                        remaining.append(tr)
                        continue
                    try:
                        r=await rpc(ws,{"proposal_open_contract":1,"contract_id":cid},101)
                        if "error" in r:
                            remaining.append(tr)
                            continue
                        poc=r.get("proposal_open_contract") or {}
                        is_sold=poc.get("is_sold") or poc.get("is_expired") or False
                        status=poc.get("status")
                        profit = poc.get("profit") or 0
                        try: profit_val=float(profit or 0)
                        except: profit_val=0.0
                        if is_sold or status in ("sold","won","lost"):
                            tg(f"{'✅' if profit_val>0 else '❌'} <b>CERRADA VOL75 1h - {'GANADA' if profit_val>0 else 'PERDIDA'}</b> | {tr.get('base')} {tr.get('direccion')} | 💰 {profit_val:+.2f} USD")
                        else:
                            remaining.append(tr)
                    except: remaining.append(tr)
            return remaining
        new_trades=[]
        try: new_trades=__import__("asyncio").run(_check())
        except: new_trades=trades
        save_open_vol75(new_trades)
    except: pass

def abrir_en_deriv(mejor):
    env=dict(os.environ)
    env.update({"DERIV_SIDE":mejor["direccion"],"DERIV_SYMBOL":mejor["deriv_sym"],"STAKE_USD":str(mejor["stake"]),"SIGNAL_ENTRY":str(mejor["precio"]),"SIGNAL_SL":str(mejor["sl"]),"SIGNAL_TP":str(mejor["tp"])})
    try:
        p=subprocess.run([sys.executable,PLACE_MULT_SCRIPT],capture_output=True,text=True,env=env,timeout=120)
        out=(p.stdout or "")+(p.stderr or "")
        cid=None
        for line in out.splitlines():
            if "contract_id=" in line:
                try: cid=line.split("contract_id=")[1].split()[0].strip()
                except: pass
        return p.returncode==0,out,cid
    except Exception as ex: return False,f"Error Deriv: {ex}",None

def abrir_vol75(mejor):
    env=dict(os.environ)
    env.update({"DERIV_SIDE":mejor["direccion"],"DERIV_SYMBOL":mejor["deriv_sym"],"STAKE_USD":str(mejor["stake"]),"DURATION":str(mejor.get("duration",1)),"DURATION_UNIT":mejor.get("duration_unit","h")})
    try:
        p=subprocess.run([sys.executable,PLACE_TURBO_SCRIPT],capture_output=True,text=True,env=env,timeout=120)
        out=(p.stdout or "")+(p.stderr or "")
        cid=None
        for line in out.splitlines():
            if "contract_id=" in line:
                try: cid=line.split("contract_id=")[1].split()[0].strip()
                except: pass
        return p.returncode==0,out,cid
    except Exception as ex: return False,f"Error Vol75: {ex}",None

def ciclo_forex():
    print(f"🔍 v22 FOREX 4 pares $20 - {len(INSTRUMENTOS)*2} señales (2h) - 60 - R:R 1:{MIN_RR} - FIX RESTART")
    check_closed_trades()
    open_trades=load_open_trades()
    real_symbols=get_open_symbols_from_deriv()
    print(f" 📂 Forex Abiertas local: {len(open_trades)}/{MAX_OPEN_TRADES} -> {[t.get('base') for t in open_trades]} | Real Deriv: {real_symbols}")
    if len(open_trades) >= MAX_OPEN_TRADES or len(real_symbols) >= MAX_OPEN_TRADES:
        print(f" ⏸️ Forex Max {MAX_OPEN_TRADES} alcanzado (local o real)")
        return
    mejor,todos=elegir_mejor()
    if mejor is None:
        print(" Sin datos forex"); return
    if mejor["confianza"]<mejor["min_score"]:
        print(f" ⏭️ Forex descartado: {mejor['nombre']} conf {mejor['confianza']} < {mejor['min_score']}")
        return
    for intento in todos:
        if intento["confianza"] < intento["min_score"]: continue
        if is_symbol_open(intento["base"]):
            print(f" ⏭️ {intento['nombre']} ya abierto (local o Deriv real), siguiente...")
            continue
        print(f"🏆 Forex Intentando: {intento['nombre']} {intento['direccion']} conf {intento['confianza']} stake ${intento['stake']}")
        if TRADE_AUTOMATIC:
            ok,salida,cid=abrir_en_deriv(intento)
            print(salida[-500:])
            if not ok and ("TradingDurationNotAllowed" in salida or "Invalid" in salida or "Unknown" in salida or "payout=0" in salida):
                continue
            if not ok:
                print(f"❌ Forex Error: {salida[-300:]}")
                return
            else:
                if cid: add_open_trade(cid,intento,100,0,0)
                else: add_open_trade(f"TEMP-{int(time.time())}",intento,100,0,0)
                tg(texto_senal(intento))
                print(f"✅ Forex Señal {intento['nombre']} - con Telegram - stake $20 - FIX RESTART")
                return

def ciclo_vol75():
    print(f"⚡ v22 VOL75 1h - R_75 - conf 70+ - stake $10 - con Telegram - FIX RESTART")
    check_closed_vol75()
    open_v=load_open_vol75()
    real_symbols=get_open_symbols_from_deriv()
    if len(open_v)>=1 or "R_75" in real_symbols:
        print(f" ⏸️ Vol75 ya abierta local o real, esperando cierre")
        return
    mejor=analizar_vol75_1h()
    if mejor is None:
        print(" ⏭️ Vol75 sin señal clara")
        return
    if mejor["confianza"]<mejor["min_score"]:
        print(f" ⏭️ Vol75 descartada conf {mejor['confianza']} < {mejor['min_score']}")
        return
    print(f"⚡ Vol75 Intentando: {mejor['direccion']} conf {mejor['confianza']} {mejor['razones']}")
    if TRADE_AUTOMATIC:
        ok,salida,cid=abrir_vol75(mejor)
        print(salida[-500:])
        if not ok:
            print(f"❌ Vol75 Error: {salida[-300:]}")
            return
        else:
            if cid: add_open_vol75(cid,mejor)
            else: add_open_vol75(f"TEMP-{int(time.time())}",mejor)
            tg(texto_senal(mejor))
            print(f"✅ Vol75 Señal {mejor['direccion']} abierta - CON TELEGRAM - stake $10 - FIX RESTART")

def main():
    print(f"✅ Bot v22 FIX RESTART iniciado. Forex 4 pares $20 + VOL75 1h $10 | R:R 1:{MIN_RR} | 60 | Max {MAX_OPEN_TRADES}")
    last_forex=0
    last_vol75=0
    while True:
        try:
            now=time.time()
            if now-last_forex>=RUN_INTERVAL_HOURS*3600:
                ciclo_forex()
                last_forex=now
            if now-last_vol75>=RUN_INTERVAL_VOL75_1H*60:
                ciclo_vol75()
                last_vol75=now
        except Exception as ex: print("[ciclo]",ex)
        time.sleep(30)

if __name__=="__main__":
    main()
