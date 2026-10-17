#!/usr/bin/env python3
"""
Starter que corre AMBOS bots en paralelo en Railway:
- Bot v25 Forex 4 pares + Vol75 (senal_diaria.py)
- Acople Damaris -> Deriv (deriv_espejo_damaris_acople.py)

Para Railway: Settings -> Start Command = python start_both_bots.py
"""
import subprocess, sys, time, os

def run_bot(name, script):
    print(f"🚀 Iniciando {name}: {script}")
    # Usa Popen para correr en paralelo
    env = dict(os.environ)
    return subprocess.Popen([sys.executable, script], env=env)

if __name__ == "__main__":
    bots = []
    # Bot 1: Forex 4 pares + Vol75 v25
    if os.path.exists("senal_diaria.py"):
        bots.append(run_bot("Forex4+Vol75 v25", "senal_diaria.py"))
    else:
        print("⚠️ senal_diaria.py no encontrado")

    # Espera 5 seg entre bots
    time.sleep(5)

    # Bot 2: Espejo Damaris -> Deriv
    if os.path.exists("deriv_espejo_damaris_acople.py"):
        bots.append(run_bot("Damaris->Deriv", "deriv_espejo_damaris_acople.py"))
    else:
        print("⚠️ deriv_espejo_damaris_acople.py no encontrado")

    print(f"✅ {len(bots)} bots iniciados, esperando...")

    # Mantiene vivo mientras ambos corran
    try:
        while True:
            for i, p in enumerate(bots):
                if p.poll() is not None:
                    print(f"⚠️ Bot {i} murio con codigo {p.returncode}, reiniciando en 10s...")
                    time.sleep(10)
                    # Reinicia segun indice
                    if i == 0 and os.path.exists("senal_diaria.py"):
                        bots[i] = run_bot("Forex4+Vol75 v25", "senal_diaria.py")
                    elif i == 1 and os.path.exists("deriv_espejo_damaris_acople.py"):
                        bots[i] = run_bot("Damaris->Deriv", "deriv_espejo_damaris_acople.py")
            time.sleep(30)
    except KeyboardInterrupt:
        print("🛑 Deteniendo bots...")
        for p in bots:
            p.terminate()
