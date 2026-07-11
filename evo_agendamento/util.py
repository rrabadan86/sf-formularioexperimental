# Funções auxiliares compartilhadas (datas, telefone, nome).
import json
import re
import unicodedata
from datetime import datetime, timedelta

import requests


def build_session():
    """Session HTTP com retry APENAS de conexão (DNS/connect) — seguro para POST,
    pois só repete quando a requisição nem chegou ao servidor. Não repete leituras."""
    session = requests.Session()
    try:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry = Retry(total=None, connect=3, read=0, redirect=0, status=0, backoff_factor=1)
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    except Exception:  # se urllib3 não expuser Retry, segue sem retry
        pass
    return session


def parse_summary_json(text):
    """Extrai o JSON do resumo do ZEE (que é configurado para devolver os dados
    do agendamento). Tolera cercas de código (```json) e texto em volta."""
    if not text:
        return None
    if isinstance(text, dict):
        return text
    s = str(text).strip()
    s = re.sub(r"^```[a-zA-Z]*", "", s).strip()
    s = re.sub(r"```$", "", s).strip()
    i, j = s.find("{"), s.rfind("}")
    if i == -1 or j == -1:
        return None
    try:
        data = json.loads(s[i:j + 1])
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


def only_digits(value) -> str:
    """Mantém apenas dígitos (telefone). Retorna '' se vazio/None."""
    if not value:
        return ""
    return re.sub(r"\D+", "", str(value))


def br_phone_with_9(value) -> str:
    """Garante o 9 em celular brasileiro (o ZEE às vezes grava sem, ex.: 8280-9212).
    Regra: depois do DDD, celular tem 9 dígitos começando com 9. Se vier com 8
    dígitos e o 1º for de celular (6-9), insere o 9. Aceita com ou sem o 55.
    Não mexe em telefone fixo (começa com 2-5) nem em número que já tem o 9."""
    d = only_digits(value)
    if d.startswith("55") and len(d) == 12 and d[4] in "6789":   # 55 + DDD + 8 (sem o 9)
        return d[:4] + "9" + d[4:]
    if len(d) == 10 and d[2] in "6789":                          # DDD + 8 (sem país, sem o 9)
        return d[:2] + "9" + d[2:]
    return d


def parse_datetime(value) -> datetime:
    """Aceita datetime ou string em vários formatos e devolve um datetime.

    Formatos aceitos: ISO ('2026-07-10T19:00'), '2026-07-10 19:00',
    'dd/mm/aaaa HH:MM' e 'dd/mm/aaaa HH:MM:SS'.
    """
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    # ISO (com ou sem segundos / 'T')
    try:
        return datetime.fromisoformat(s.replace("Z", "").strip())
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Data/hora não reconhecida: {value!r}")


def fmt_datetime_evo(value) -> str:
    """Formato aceito pelo EVO no agendamento: 'yyyy-MM-dd HH:mm'."""
    return parse_datetime(value).strftime("%Y-%m-%d %H:%M")


def fmt_date_evo(value) -> str:
    """Formato de data (sem hora) para filtros do EVO: 'yyyy-MM-dd'."""
    return parse_datetime(value).strftime("%Y-%m-%d")


_WEEKDAY_PT = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
               "sexta-feira", "sábado", "domingo"]


def friendly_when_ptbr(value) -> str:
    """Data amigável para a mensagem à aluna: 'sexta-feira, 10/07 às 16:15'."""
    dt = parse_datetime(value)
    return f"{_WEEKDAY_PT[dt.weekday()]}, {dt.strftime('%d/%m')} às {dt.strftime('%H:%M')}"


# ---------------- interpretação de data em português (linguagem natural) ----------------
# A aluna costuma dizer "segunda às 8h15" (dia da semana + hora), raramente "dia 17".
# Convertemos para a data real (yyyy-MM-dd HH:mm) que o EVO espera.
_WEEKDAYS = {  # Python: segunda=0 ... domingo=6
    "segunda-feira": 0, "segunda": 0, "seg": 0,
    "terca-feira": 1, "terca": 1, "ter": 1,
    "quarta-feira": 2, "quarta": 2, "qua": 2,
    "quinta-feira": 3, "quinta": 3, "qui": 3,
    "sexta-feira": 4, "sexta": 4, "sex": 4,
    "sabado": 5, "sab": 5,
    "domingo": 6, "dom": 6,
}


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def _extract_time(t: str):
    """Extrai (hora, minuto) de trechos tipo '8h15', '08:15', '8h', 'às 20h30', '8 horas'."""
    for pat in (r"(\d{1,2})\s*[:h]\s*(\d{2})",   # 8h15 / 08:15
                r"(\d{1,2})\s*[:h]",             # 8h / 8:
                r"\bas\s+(\d{1,2})\b",           # as 8
                r"(\d{1,2})\s*horas?"):          # 8 horas
        m = re.search(pat, t)
        if m:
            hh = int(m.group(1))
            mm = int(m.group(2)) if m.lastindex and m.lastindex >= 2 and m.group(2) else 0
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                return hh, mm
    return None, None


def _next_weekday(now, weekday, hh, mm):
    days = (weekday - now.weekday()) % 7
    cand = (now + timedelta(days=days)).replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand <= now:                      # mesmo dia, mas a hora já passou -> semana que vem
        cand += timedelta(days=7)
    return cand


def parse_when_ptbr(text, now=None) -> datetime:
    """Converte 'segunda às 8h15', 'amanhã 07:00', 'dia 17 às 8h' em datetime real.
    Levanta ValueError se não conseguir identificar data + hora."""
    now = now or datetime.now()
    t = _strip_accents(str(text)).lower()
    hh, mm = _extract_time(t)
    if hh is None:
        raise ValueError(f"Não identifiquei a hora em: {text!r}")

    base = None
    if "depois de amanha" in t:
        base = now.date() + timedelta(days=2)
    elif "amanha" in t:
        base = now.date() + timedelta(days=1)
    elif "hoje" in t:
        base = now.date()

    if base is not None:
        return datetime(base.year, base.month, base.day, hh, mm)

    # dia da semana (segunda, terça, ...)
    for kw, wd in _WEEKDAYS.items():
        if re.search(rf"\b{kw}\b", t):
            return _next_weekday(now, wd, hh, mm)

    # "dia 17" (ou um número de dia solto) -> dia do mês atual; se já passou, próximo mês
    m = re.search(r"\bdia\s+(\d{1,2})\b", t) or re.search(r"\b(\d{1,2})\b(?!\s*[:h])", t)
    if m:
        day = int(m.group(1))
        cand = _safe_date(now.year, now.month, day, hh, mm)
        if cand and cand <= now:
            year = now.year + (1 if now.month == 12 else 0)
            month = 1 if now.month == 12 else now.month + 1
            cand = _safe_date(year, month, day, hh, mm)
        if cand:
            return cand

    raise ValueError(f"Não identifiquei o dia em: {text!r}")


def _safe_date(year, month, day, hh, mm):
    try:
        return datetime(year, month, day, hh, mm)
    except ValueError:
        return None


def parse_when(value, now=None) -> datetime:
    """Aceita datetime, formato estruturado (ISO/br) OU texto em português."""
    if isinstance(value, datetime):
        return value
    try:
        return parse_datetime(value)          # ISO / dd-mm-aaaa / etc.
    except ValueError:
        return parse_when_ptbr(value, now)     # linguagem natural


def split_name(full_name: str):
    """Divide 'Nome Completo' em (primeiro nome, sobrenome) para o EVO."""
    parts = (full_name or "").strip().split()
    if not parts:
        return "", None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " ".join(parts[1:])


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def extract_email(text: str):
    """Extrai o primeiro e-mail de um texto livre (fallback quando não vem estruturado)."""
    if not text:
        return None
    m = _EMAIL_RE.search(text)
    return m.group(0) if m else None


# ---------------- disponibilidade de turma (aula experimental) ----------------
def session_start_datetime(sess: dict):
    """Datetime de início de um horário retornado pelo EVO.

    Usa activityDate (que já traz a hora); se vier só a data, combina com startTime."""
    raw = sess.get("activityDate")
    if not raw:
        return None
    try:
        dt = parse_datetime(raw)
    except (ValueError, TypeError):
        return None
    start = sess.get("startTime")
    if dt.hour == 0 and dt.minute == 0 and start:
        try:
            h, m = str(start).split(":")[:2]
            dt = dt.replace(hour=int(h), minute=int(m))
        except (ValueError, TypeError):
            pass
    return dt


def session_free_spots(sess: dict):
    """Vagas restantes na turma (min entre capacidade geral e vagas de experimental).
    Retorna None quando a capacidade é ilimitada/desconhecida."""
    capacity = sess.get("capacity")
    ocupation = sess.get("ocupation") or 0
    exp_slots = sess.get("experimentalClassSlots")
    livres = []
    if capacity is not None:
        livres.append(max(capacity - ocupation, 0))
    if exp_slots is not None:
        livres.append(max(exp_slots, 0))
    return min(livres) if livres else None


def session_has_room(sess: dict) -> bool:
    """True se a turma aceita aula experimental e ainda tem vaga (usa o flag)."""
    if not sess.get("allowExperimentalClass"):
        return False
    livres = session_free_spots(sess)
    return livres is None or livres > 0


def session_free_general(sess: dict):
    """Vagas restantes pela capacidade da turma (ignora o flag de experimental)."""
    capacity = sess.get("capacity")
    if capacity is None:
        return None
    return max(capacity - (sess.get("ocupation") or 0), 0)


def session_has_room_normal(sess: dict) -> bool:
    """True se a turma tem vaga por capacidade (agendamento normal): ocupation < capacity."""
    livres = session_free_general(sess)
    return livres is None or livres > 0


def same_slot(sess: dict, when) -> bool:
    """True se o horário da sessão bate com o desejado (mesma data e HH:MM)."""
    start = session_start_datetime(sess)
    if start is None:
        return False
    alvo = parse_datetime(when)
    return (start.date() == alvo.date()
            and start.hour == alvo.hour
            and start.minute == alvo.minute)
