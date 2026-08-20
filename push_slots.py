"""Calcula a grade de horários NO VPS e ENVIA pronta para o formulário (Render).

Por quê: montar a grade faz uma chamada ao EVO por horário livre. No VPS (sempre
ligado, rápido e perto do EVO) isso leva segundos; na Render free é lento e a
aluna esperava. Aqui o VPS calcula e faz POST /api/slots/push; o formulário passa
a servir essa grade NA HORA, sem tocar no EVO.

Agendado pelo scheduler do robô (5h da manhã + a cada poucos minutos no horário
comercial). Também pode rodar à mão:  python3 push_slots.py

Variáveis de ambiente usadas (do mesmo .env do robô):
  FORM_CLOUD_URL     -> URL do formulário (ex.: https://sf-formularioexperimental.onrender.com)
  FORM_SLOTS_TOKEN   -> token secreto (o MESMO valor no VPS e na Render)
  FORM_DAYS          -> janela de dias (padrão 10)
  FORM_MAX_OCUPACAO  -> ocupação máxima p/ "disponível" (padrão 7)
  (EVO_DNS / EVO_TOKEN etc. — os mesmos que o agendamento já usa)
"""
import os
import sys
import time

import requests

from evo_agendamento import available_slots

FORM_URL = (os.getenv("FORM_CLOUD_URL", "") or "").rstrip("/")
TOKEN = os.getenv("FORM_SLOTS_TOKEN", "")
DAYS = int(os.getenv("FORM_DAYS", "10"))
MAXOC = int(os.getenv("FORM_MAX_OCUPACAO", "7"))


def main():
    if not FORM_URL or not TOKEN:
        print("push_slots: FORM_CLOUD_URL e/ou FORM_SLOTS_TOKEN ausentes no .env — nada enviado.")
        return 1
    t0 = time.monotonic()
    # use_cache=False: queremos a grade fresca de verdade para enviar.
    slots = available_slots(days=DAYS, max_ocupacao=MAXOC, use_cache=False)
    dt = time.monotonic() - t0
    try:
        r = requests.post(
            f"{FORM_URL}/api/slots/push",
            params={"token": TOKEN},
            json={"slots": slots, "days": DAYS, "maxOcupacao": MAXOC},
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"push_slots: falha ao enviar ({type(e).__name__}: {e})")
        return 2
    print(f"push_slots: {len(slots)} horários calculados em {dt:.1f}s -> HTTP {r.status_code} {r.text[:160]}")
    return 0 if r.ok else 3


if __name__ == "__main__":
    sys.exit(main())
