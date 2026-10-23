# -*- coding: utf-8 -*-
"""
Limitador de taxa do EVO COMPARTILHADO entre processos.

O limite do EVO é de 40 req/min POR CONTA. Cada processo tem o seu próprio
limitador em memória (evo_client._rate_gate), mas isso não coordena processos
diferentes — quando dois jobs (ex.: grade + push de slots, ou duas lojas no
mesmo servidor com a MESMA conta EVO) batem ao mesmo tempo, a soma passa de 40
e o EVO devolve 429.

Este módulo mantém uma janela deslizante de timestamps num arquivo com lock
(flock), por CONTA EVO (hash do DNS). Todos os processos Python na mesma máquina
que usam a mesma conta passam por aqui, então o total fica abaixo do limite.

Best-effort: qualquer falha no limitador (lock, disco) NUNCA trava o processo —
cai no limitador em memória do evo_client, que continua valendo.
"""
import hashlib
import json
import os
import tempfile
import time

try:
    import fcntl  # POSIX (Linux) — no VPS sempre existe
except ImportError:                       # pragma: no cover
    fcntl = None

_RATE_MAX = 38          # folga sob o limite de 40/min do EVO
_WINDOW = 60.0


def _path(dns):
    h = hashlib.sha1(str(dns or "default").encode("utf-8")).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"evo-rate-{h}.json")


def acquire(dns, max_per_min=_RATE_MAX, window=_WINDOW, timeout=120.0):
    """Bloqueia até liberar uma 'ficha' na janela compartilhada da conta `dns`.
    Sem fcntl (SO sem flock) ou em qualquer erro, retorna na hora (best-effort)."""
    if fcntl is None:
        return
    path = _path(dns)
    deadline = time.monotonic() + timeout
    while True:
        espera = None
        try:
            with open(path, "a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.seek(0)
                raw = f.read().strip()
                try:
                    hits = json.loads(raw) if raw else []
                    if not isinstance(hits, list):
                        hits = []
                except Exception:
                    hits = []
                now = time.time()
                hits = [t for t in hits if isinstance(t, (int, float)) and now - t < window]
                if len(hits) < max_per_min:
                    hits.append(now)
                    f.seek(0)
                    f.truncate()
                    f.write(json.dumps(hits))
                    return                 # ficha obtida (lock sai no fim do with)
                espera = max(0.05, window - (now - min(hits)) + 0.03)
        except Exception:
            return                         # best-effort: nunca trava o processo
        if espera is None or time.monotonic() >= deadline:
            return
        time.sleep(min(espera, 5.0))
