#!/usr/bin/env python3
"""
Starter para Railway.

ESTADO ACTUAL (30/10/2026): FOREX APAGADO.
Solo corre el Acople Damaris/XCLUB/SNIPER/UK -> Deriv.

El bot de forex v25 (senal_diaria.py) queda DESACTIVADO porque con stake $20 y
multiplicador x100 un solo SL cuesta -$18 (el 90% del stake), y en 40 horas de
operacion se comio -18.03 de los +38.57 que ganaron los canales de Telegram.

Para volver a encenderlo: poner FOREX_ON=1 en las variables de Railway
(o cambiar la variable de abajo FOREX_ON = "1").
Vol75 (R_75) vive DENTRO de senal_diaria.py, asi que tambien queda apagado.
"""
import subprocess, sys, time, os

FOREX_ON = os.environ.get("FOREX_ON", "0")   # "1" = encender forex v25
FOREX_SCRIPT = "senal_diaria.py"
ACOPLE_SCRIPT = "deriv_espejo_damaris_acople.py"

def run_bot(name, script):
    print(f"🚀 Iniciando {name}: {script}", flush=True)
    return subprocess.Popen([sys.executable, script], env=dict(os.environ))

if __name__ == "__main__":
    bots = []   # lista de (nombre, script, proceso)

    # ---------- Bot 1: Forex v25 + Vol75 (APAGADO por defecto) ----------
    if FOREX_ON == "1":
        if os.path.exists(FOREX_SCRIPT):
            bots.append(("Forex4+Vol75 v25", FOREX_SCRIPT, run_bot("Forex4+Vol75 v25", FOREX_SCRIPT)))
        else:
            print(f"⚠️ {FOREX_SCRIPT} no encontrado", flush=True)
        time.sleep(5)
    else:
        print("⏸️ FOREX v25 APAGADO (FOREX_ON=0). No se opera forex ni Vol75.", flush=True)

    # ---------- Bot 2: Acople Telegram -> Deriv (SIEMPRE) ----------
    if os.path.exists(ACOPLE_SCRIPT):
        bots.append(("Telegram->Deriv", ACOPLE_SCRIPT, run_bot("Telegram->Deriv", ACOPLE_SCRIPT)))
    else:
        print(f"⚠️ {ACOPLE_SCRIPT} no encontrado", flush=True)

    print(f"✅ {len(bots)} bot(s) iniciados: " + ", ".join(b[0] for b in bots), flush=True)

    try:
        while True:
            for i, (name, script, p) in enumerate(bots):
                if p.poll() is not None:
                    print(f"⚠️ {name} murio con codigo {p.returncode}, reiniciando en 10s...", flush=True)
                    time.sleep(10)
                    bots[i] = (name, script, run_bot(name, script))
            time.sleep(30)
    except KeyboardInterrupt:
        print("🛑 Deteniendo bots...", flush=True)
        for _, _, p in bots:
            p.terminate()
