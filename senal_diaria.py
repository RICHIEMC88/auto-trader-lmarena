#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOT v5 MIXTA - AUDNZD diario 60 + BTC/XAU scalping 1h 70 + solo abre/cierra
- AUDNZD: velas diarias, MIN_SCORE 60 (baja volatilidad, funciona con x100)
- BTC/XAU: velas 1h scalping, MIN_SCORE 70 (alta volatilidad, evita falsas, SL 0.5% cabe en x100)
- Solo Telegram cuando abre y cierra, menciona estrategia
- Fix multiplier 100 (Deriv solo acepta 100,200,300...)
"""
import os, sys, time, json, subprocess, requests, pandas as pd, yfinance as yf

def e(k,d=None): return os.environ.get(k,d)
TG_TOKEN=e("TELEGRAM_BOT_TOKEN","").strip()
TG_CHAT=e("TELEGRAM_CHAT_ID","").strip()
TRADE_AUTOMATIC=e("TRADE_AUTOMATIC","true").lower() in ("1","true","yes")
RUN_INTERVAL_HOURS=int(e("RUN_INTERVAL_HOURS","2"))
MIN_RR=float(e("MIN_RR","3.0"))

# CONFIG MIXTA: timeframe y score por instrumento
INSTRUMENTOS={
 "AUDNZD": {"ticker":"AUDNZD=X","deriv_sym":"frxAUDNZD","stake":float(e("STAKE_AUDNZD",e("STAKE_USD","10"))),"dec":5,"interval":"1d","min_score":60,"period":"8mo"},
 "BTC":    {"ticker":"BTC-USD","deriv_sym":"cryBTCUSD","stake":float(e("STAKE_BTC",e("STAKE_USD","10"))),"dec":1,"interval":"1h","min_score":70,"period":"1mo"},
 "XAU":    {"ticker":"GC=F","deriv_sym":"frxXAUUSD","stake":float(e("STAKE_XAU",e("STAKE_USD","10"))),"dec":2,"interval":"1h","min_score":70,"period":"1mo"},
}

SCRIPT_DIR=os.path.dirname(os.path.abspath(__file__))
PLACE_SCRIPT=os.path.join(SCRIPT_DIR,"deriv_place_signal.py")
OPEN_TRADES_FILE=os.path.join(SCRIPT_DIR,"open_trades.json")

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
def get_data(ticker, interval="1d", period="8mo"):
    try: return yf.Ticker(ticker).history(period=period, interval=interval)
    except: return None

def analizar_profesional(ticker, interval="1d", period="8mo"):
    df=get_data(ticker, interval, period)
    if df is None or len(df)<60: return None
    close=df["Close"]; last=float(close.iloc[-1]); s20=float(sma(close,20).iloc[-1]); s50=float(sma(close,50).iloc[-1]); s200=float(sma(close,200).iloc[-1]) if len(close)>=200 else float(sma(close,50).iloc[-1])
    r=float(rsi(close).iloc[-1]); a=float(atr(df).iloc[-1]); _,_,mh=macd(close); hist=float(mh.iloc[-1]); hist_prev=float(mh.iloc[-2]) if len(mh)>=2 else hist
    slw,sh=swing_extremes(df, 10 if interval=="1d" else 6, 2)
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
    # SL mas ajustado para scalping 1h: clamp 0.4-1.0 ATR en 1h vs 0.6-1.5 en diario
    if interval=="1h":
        min_sl=0.4*a; max_sl=1.0*a
    else:
        min_sl=0.6*a; max_sl=1.5*a
    if direc=="BUY":
        sl_c=slw-0.15*a; risk=last-sl_c; sl=last-min_sl if risk<min_sl else last-max_sl if risk>max_sl else sl_c; risk=last-sl
    else:
        sl_c=sh+0.15*a; risk=sl_c-last; sl=last+min_sl if risk<min_sl else last+max_sl if risk>max_sl else sl_c; risk=sl-last
    if risk<=0: return None
    tp=last+(risk*MIN_RR) if direc=="BUY" else last-(risk*MIN_RR)
    return {"precio":last,"s20":s20,"s50":s50,"s200":s200,"rsi":r,"atr":a,"hist":hist,"swing_low":slw,"swing_high":sh,"direccion":direc,"sl":sl,"tp":tp,"rr":MIN_RR,"confianza":conf,"razones":raz,"estrategia":"PROFESIONAL","interval":interval}

def analizar_institucional(ticker, interval="1d", period="8mo"):
    df=get_data(ticker, interval, period)
    if df is None or len(df)<80: return None
    close=df["Close"]; high=df["High"]; low=df["Low"]; op=df["Open"]
    last=float(close.iloc[-1]); last_high=float(high.iloc[-1]); last_low=float(low.iloc[-1]); s20=float(sma(close,20).iloc[-1]); a=float(atr(df).iloc[-1])
    slw,sh=swing_extremes(df, 15 if interval=="1d" else 8, 2)
    high_20=float(high.iloc[-20:-1].max()); low_20=float(low.iloc[-20:-1].min()); prev_high=float(high.iloc[-2]); prev_low=float(low.iloc[-2]); prev_close=float(close.iloc[-2])
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
    if sweep_bull: bu+=3; rb.append("🧹 Barrida liquidez bajista (ballenas)")
    if fvg_bull: bu+=2.5; rb.append("📦 FVG alcista")
    if ob_bull: bu+=2.5; rb.append("🏦 Order Block alcista")
    if bias_bull: bu+=2; rb.append("🏛️ Sesgo institucional alcista")
    if last>high_20: bu+=1.5; rb.append("🔓 Ruptura liquidez high 20")
    if prev_close<low_20 and last>low_20: bu+=2; rb.append("🔄 Reclamo liquidez")
    re,be=[],0.0
    if sweep_bear: be+=3; re.append("🧹 Barrida liquidez alcista")
    if fvg_bear: be+=2.5; re.append("📦 FVG bajista")
    if ob_bear: be+=2.5; re.append("🏦 Order Block bajista")
    if bias_bear: be+=2; re.append("🏛️ Sesgo institucional bajista")
    if last<low_20: be+=1.5; re.append("🔓 Ruptura liquidez low 20")
    if prev_close>high_20 and last<high_20: be+=2; re.append("🔄 Rechazo liquidez")
    diff=bu-be; conf=round(min(100.0,abs(diff)/12.0*100),1); direc="BUY" if diff>=0 else "SELL"; raz=rb if direc=="BUY" else re
    if interval=="1h":
        min_sl=0.4*a; max_sl=1.0*a
    else:
        min_sl=0.7*a; max_sl=1.8*a
    if direc=="BUY":
        sl_c=min(slw,low_20)-0.2*a; risk=last-sl_c; sl=last-min_sl if risk<min_sl else last-max_sl if risk>max_sl else sl_c; risk=last-sl
    else:
        sl_c=max(sh,high_20)+0.2*a; risk=sl_c-last; sl=last+min_sl if risk<min_sl else last+max_sl if risk>max_sl else sl_c; risk=sl-last
    if risk<=0: return None
    tp=last+(risk*MIN_RR) if direc=="BUY" else last-(risk*MIN_RR)
    return {"precio":last,"s20":s20,"atr":a,"swing_low":slw,"swing_high":sh,"direccion":direc,"sl":sl,"tp":tp,"rr":MIN_RR,"confianza":conf,"razones":raz,"estrategia":"INSTITUCIONAL","interval":interval,"high_20":high_20,"low_20":low_20}

def elegir_mejor():
    res=[]
    for key,cfg in INSTRUMENTOS.items():
        r1=analizar_profesional(cfg["ticker"], cfg["interval"], cfg["period"])
        if r1: r1["nombre"]=f"{key} [PRO] {cfg['interval']}"; r1["base"]=key; r1["deriv_sym"]=cfg["deriv_sym"]; r1["stake"]=cfg["stake"]; r1["dec"]=cfg["dec"]; r1["min_score"]=cfg["min_score"]; r1["interval"]=cfg["interval"]; res.append(r1)
        r2=analizar_institucional(cfg["ticker"], cfg["interval"], cfg["period"])
        if r2: r2["nombre"]=f"{key} [INST] {cfg['interval']}"; r2["base"]=key; r2["deriv_sym"]=cfg["deriv_sym"]; r2["stake"]=cfg["stake"]; r2["dec"]=cfg["dec"]; r2["min_score"]=cfg["min_score"]; r2["interval"]=cfg["interval"]; res.append(r2)
    if not res: return None,[]
    # Filtra por min_score propio de cada instrumento
    validos=[r for r in res if r["confianza"]>=r["min_score"]]
    if not validos:
        # Si ninguno pasa, devuelve el mejor de todos para log, pero no abrira
        mejor=max(res,key=lambda x:x["confianza"])
        return mejor, res
    mejor=max(validos,key=lambda x:x["confianza"])
    return mejor, res

def fmt_px(v,d): return f"{v:,.{d}f}"
def texto_senal(mejor,todos,abierto):
    s=mejor; riesgo=abs(s["precio"]-s["sl"]); benef=abs(s["tp"]-s["precio"]); rr=round(benef/riesgo,2) if riesgo else 0
    return "\n".join([
        f"🚀 <b>SEÑAL ABIERTA - {s['nombre']}</b>",f"🧠 Estrategia: <b>{s['estrategia']}</b> {'(Ballenas SMC)' if s['estrategia']=='INSTITUCIONAL' else '(Tendencia)'} | ⏰ {s['interval']}",
        "━━━━━━━━━━━━━━━━━━━━",f"🎯 Dirección: <b>{'COMPRA' if s['direccion']=='BUY' else 'VENTA'}</b>",f"📈 Entrada: <b>{fmt_px(s['precio'], s['dec'])}</b>",
        f"🛑 SL: {fmt_px(s['sl'], s['dec'])} | 🎯 TP: {fmt_px(s['tp'], s['dec'])}",f"📐 R:R 1:{rr:.2f} | 📊 Conf: {s['confianza']}/{s['min_score']} | ATR {fmt_px(s['atr'], s['dec'])}",
        "━━━━━━━━━━━━━━━━━━━━","🧠 Por qué?"] + [f"   ✅ {rz}" for rz in s["razones"]] + ["━━━━━━━━━━━━━━━━━━━━", f"✅ <b>Orden abierta Deriv DEMO DOT91988837 | Estrategia {s['estrategia']} | {s['interval']}</b>" if abierto else f"⚠️ Fallo apertura [{s['estrategia']}]"])

def texto_cierre(trade, profit, is_win):
    s=trade; emoji="✅" if is_win else "❌"; res="GANADA" if is_win else "PERDIDA"
    return "\n".join([
        f"{emoji} <b>OPERACIÓN CERRADA - {res}</b>",f"🧠 Estrategia: <b>{s.get('estrategia','?')}</b> | ⏰ {s.get('interval','?')}","━━━━━━━━━━━━━━━━━━━━",
        f"📊 {s.get('nombre','?')} {s.get('direccion','?')} | Entrada {fmt_px(s.get('precio',0), s.get('dec',2))}",
        f"🛑 SL: {fmt_px(s.get('sl',0), s.get('dec',2))} | 🎯 TP: {fmt_px(s.get('tp',0), s.get('dec',2))}",
        f"💰 Resultado: <b>{profit:+.2f} USD</b> ({'TP' if is_win else 'SL'})",f"🆔 {s.get('contract_id','?')} | Stake {s.get('stake','?')} x{s.get('mult','?')}",
        f"⏰ Abierta: {s.get('open_time','?')} | Cerrada: {time.strftime('%Y-%m-%d %H:%M')}",
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
    tr.append({"contract_id":cid,"nombre":mejor["nombre"],"base":mejor["base"],"deriv_sym":mejor["deriv_sym"],"direccion":mejor["direccion"],"precio":mejor["precio"],"sl":mejor["sl"],"tp":mejor["tp"],"stake":mejor["stake"],"dec":mejor["dec"],"mult":mult,"sl_usd":sl_usd,"tp_usd":tp_usd,"estrategia":mejor["estrategia"],"confianza":mejor["confianza"],"interval":mejor["interval"],"min_score":mejor["min_score"],"open_time":time.strftime("%Y-%m-%d %H:%M:%S")})
    save_open_trades(tr)

def check_closed_trades():
    trades=load_open_trades()
    if not trades: return
    print(f"🔍 Revisando {len(trades)} trades...")
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
                    try:
                        r=await rpc(ws,{"proposal_open_contract":1,"contract_id":cid},101)
                        if "error" in r:
                            tg(texto_cierre(tr,0.0,False)+"\n⚠️ P&L no disponible")
                            continue
                        poc=r.get("proposal_open_contract") or {}
                        is_sold=poc.get("is_sold") or poc.get("is_expired") or False
                        status=poc.get("status")
                        profit=poc.get("profit")
                        if is_sold or status in ("sold","won","lost"):
                            try: profit_val=float(profit or 0)
                            except: profit_val=0.0
                            is_win=profit_val>0
                            tg(texto_cierre(tr,profit_val,is_win))
                        else:
                            remaining.append(tr)
                    except: remaining.append(tr)
            return remaining
        new_trades=[]
        try: new_trades=__import__("asyncio").run(_check())
        except: new_trades=trades
        save_open_trades(new_trades)
    except: pass

def abrir_en_deriv(mejor):
    env=dict(os.environ)
    env.update({"DERIV_SIDE":mejor["direccion"],"DERIV_SYMBOL":mejor["deriv_sym"],"STAKE_USD":str(mejor["stake"]),"SIGNAL_ENTRY":str(mejor["precio"]),"SIGNAL_SL":str(mejor["sl"]),"SIGNAL_TP":str(mejor["tp"])})
    try:
        p=subprocess.run([sys.executable,PLACE_SCRIPT],capture_output=True,text=True,env=env,timeout=120)
        out=(p.stdout or "")+(p.stderr or "")
        cid=None
        for line in out.splitlines():
            if "contract_id=" in line:
                try: cid=line.split("contract_id=")[1].split()[0].strip()
                except: pass
        return p.returncode==0,out,cid
    except Exception as ex: return False,f"Error Deriv: {ex}",None

def ciclo():
    print(f"🔍 Analizando 6 señales (2h) - AUDNZD 1d 60 + BTC/XAU 1h 70")
    check_closed_trades()
    mejor,todos=elegir_mejor()
    if mejor is None:
        print(" Sin datos"); return
    # Si no pasa su propio min_score, no avisa (solo abre/cierra)
    if mejor["confianza"]<mejor["min_score"]:
        print(f" ⏭️ descartado: {mejor['nombre']} conf {mejor['confianza']} < {mejor['min_score']} [{mejor['estrategia']}] - NO Telegram")
        return
    print(f"🏆 Mejor: {mejor['nombre']} {mejor['direccion']} conf {mejor['confianza']}/{mejor['min_score']} [{mejor['estrategia']}] {mejor['interval']}")
    if TRADE_AUTOMATIC:
        tg(f"🚀 <b>Abriendo DEMO...</b>\n{mejor['nombre']} {'COMPRA' if mejor['direccion']=='BUY' else 'VENTA'}\nEstrategia: <b>{mejor['estrategia']}</b> | {mejor['interval']} | Conf {mejor['confianza']}/{mejor['min_score']}")
        ok,salida,cid=abrir_en_deriv(mejor)
        print(salida[-800:])
        if not ok:
            tg(f"❌ <b>Error abrir [{mejor['estrategia']}]:</b>\n<code>{salida[-500:]}</code>")
        else:
            if cid: add_open_trade(cid,mejor,100,0,0)
            else: add_open_trade(f"TEMP-{int(time.time())}",mejor,100,0,0)
            tg(texto_senal(mejor,todos,True))

def main():
    print(f"✅ Bot v5 MIXTA iniciado. Intervalo {RUN_INTERVAL_HOURS}h | Auto {TRADE_AUTOMATIC}")
    print("   AUDNZD 1d 60 + BTC/XAU 1h 70 | Solo avisa abre/cierra con estrategia")
    while True:
        try: ciclo()
        except Exception as ex:
            print("[ciclo]",ex)
        time.sleep(RUN_INTERVAL_HOURS*3600)

if __name__=="__main__":
    main()
