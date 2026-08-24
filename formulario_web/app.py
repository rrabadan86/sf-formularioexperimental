"""Servidor do formulário de aula experimental (para hospedar no Render).

Fluxo:
  1. A página (index.html) mostra os campos + a grade de 10 dias (GET /api/slots).
  2. A aluna escolhe uma turma com vaga (ocupation <= 7) e envia (POST /api/book).
  3. O servidor cadastra + vende + matricula no EVO (reusa o pacote evo_agendamento)
     e enfileira a confirmação numa "outbox" na nuvem.
  4. O PC do Studio puxa essa outbox (GET /api/outbox/pending) e envia a confirmação
     pelo WhatsApp do Studio (8550-8065) — mesmo esquema de sempre.

Segredos (EVO_DNS, EVO_TOKEN, etc.) vêm de variáveis de ambiente (no Render, em
"Environment"). A chave do EVO NUNCA vai para o navegador.
"""
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta

from flask import Flask, jsonify, request, send_from_directory

from evo_agendamento import EvoClient, TurmaLotadaError, available_slots, book_experimental
from evo_agendamento import config
from evo_agendamento.orchestrator import _confirm_message
from evo_agendamento.util import br_phone_with_9, only_digits

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE, "static"), static_url_path="/static")

# ---- configuração via ambiente ----
FORM_DAYS = int(os.getenv("FORM_DAYS", "10"))                 # janela de dias visível
FORM_MAX_OCUPACAO = int(os.getenv("FORM_MAX_OCUPACAO", "7"))  # turma com >7 fica indisponível
# token que o PC usa para puxar a outbox (defina o MESMO valor no PC e no Render):
OUTBOX_TOKEN = os.getenv("FORM_OUTBOX_TOKEN", "")
OUTBOX_FILE = os.getenv("FORM_OUTBOX_FILE", "web_outbox.jsonl")
# indicadores (acessos/agendamentos) que o VPS puxa e persiste:
IND_FILE = os.getenv("FORM_IND_FILE", "web_indicadores.jsonl")
# token compartilhado com a Sofia (WhatsApp). Defina no Render em Environment.
SOFIA_TOKEN = os.getenv("SOFIA_TOKEN", "")
# mensagem quando a pessoa já tem aula experimental (1 por pessoa):
BLOCK_MSG = os.getenv(
    "FORM_BLOCK_MSG",
    "Vimos que você já tem uma aula experimental com a gente 😊 Para agendar uma nova, "
    "fale com a Sofia no WhatsApp (é só tocar no botão verde aqui embaixo).",
)

_lock = threading.Lock()


# =================== outbox da nuvem (fila de confirmações) ===================
def _outbox_append(row):
    with _lock:
        with open(OUTBOX_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _outbox_read_all():
    if not os.path.exists(OUTBOX_FILE):
        return []
    with open(OUTBOX_FILE, encoding="utf-8") as f:
        out = []
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return out


def _outbox_ack(keys):
    keys = set(keys or [])
    with _lock:
        rows = _outbox_read_all()
        for r in rows:
            if _row_key(r) in keys:
                r["status"] = "sent"
        with open(OUTBOX_FILE, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _row_key(row):
    return f"{row.get('contactId')}|{row.get('when')}|{row.get('ts')}"


# =================== indicadores (acessos / agendamentos) ====================
# Buffer leve que o VPS puxa (GET /api/ind/pending) e confirma (POST /api/ind/ack).
# Cada evento: {id, tipo: 'acesso'|'agendou', ts, origem}. Disco efemero da Render
# nao e problema: o VPS puxa a cada ~2 min e persiste do lado dele.
def _hora_de(when):
    m = re.search(r"(\d{1,2}:\d{2})", str(when or ""))
    return m.group(1) if m else ""


def _ind_append(tipo, origem="", extra=None):
    try:
        ev = {
            "id": uuid.uuid4().hex[:12],
            "tipo": tipo,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "origem": (origem or "")[:40],
        }
        if extra:
            ev.update(extra)
        with _lock:
            with open(IND_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        app.logger.exception("falha ao registrar indicador")


def _ind_read_all():
    if not os.path.exists(IND_FILE):
        return []
    with open(IND_FILE, encoding="utf-8") as f:
        out = []
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return out


def _ind_remove(ids):
    ids = set(ids or [])
    with _lock:
        rows = [r for r in _ind_read_all() if r.get("id") not in ids]
        with open(IND_FILE, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _ja_tem_experimental(evo, id_prospect):
    """True se o prospect já tem o serviço da aula experimental vendido (por id ou nome)."""
    sid = str(getattr(config, "EVO_SERVICE_ID", "") or "")
    for s in (evo.prospect_services(id_prospect) or []):
        if sid and str(s.get("idService")) == sid:
            return True
        if "experimental" in (s.get("nameService") or "").lower():
            return True
    return False


# =============================== validações ==================================
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _cpf_valido(cpf: str) -> bool:
    """Validação oficial do CPF (dígitos verificadores)."""
    c = only_digits(cpf)
    if len(c) != 11 or c == c[0] * 11:
        return False
    for tam in (9, 10):
        soma = sum(int(c[i]) * ((tam + 1) - i) for i in range(tam))
        dv = (soma * 10) % 11
        if dv == 10:
            dv = 0
        if dv != int(c[tam]):
            return False
    return True


def _valida(dados):
    erros = {}
    nome = (dados.get("nome") or "").strip()
    if len(nome.split()) < 2:
        erros["nome"] = "Informe o nome completo."
    cpf = only_digits(dados.get("cpf"))
    if len(cpf) != 11:
        erros["cpf"] = "CPF deve ter 11 dígitos."
    elif not _cpf_valido(cpf):
        # Antes bastava ter 11 dígitos: um número inventado passava aqui e o EVO
        # descartava o campo em silêncio, deixando o cadastro sem CPF. Agora
        # avisamos na hora, em vez de perder o dado.
        erros["cpf"] = "CPF inválido. Confira os números."
    tel = only_digits(dados.get("telefone"))
    if tel.startswith("55") and len(tel) > 11:
        tel = tel[2:]                      # tolera quem digita o 55 na frente
    if len(tel) != 11 or tel[2] != "9":
        # Todo celular brasileiro tem 11 dígitos (DDD + 9 + 8) desde 2016. Antes
        # aceitávamos 10 dígitos e um ajuste automático inseria o 9 — um número
        # digitado com 1 dígito a menos virava OUTRO número, válido na aparência
        # e inexistente no WhatsApp (a confirmação nunca chegava). Melhor avisar.
        erros["telefone"] = "Celular deve ter 11 dígitos com DDD (ex.: 62 99999-9999)."
    email = (dados.get("email") or "").strip().lower()
    if not _EMAIL_RE.match(email):
        erros["email"] = "E-mail inválido."
    nasc = (dados.get("nascimento") or "").strip()   # yyyy-MM-dd (input date)
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", nasc):
        erros["nascimento"] = "Data de nascimento inválida."
    if not (dados.get("idConfiguration") and dados.get("activityDate")):
        erros["turma"] = "Escolha uma turma."
    return erros, {"nome": nome, "cpf": cpf, "telefone": tel, "email": email, "nascimento": nasc}


# ================================= rotas =====================================
@app.get("/")
def index():
    _ind_append("acesso", request.args.get("origem") or request.args.get("utm_source") or "")
    return send_from_directory(os.path.join(BASE, "templates"), "index.html")


@app.get("/manual")
def manual():
    """Manual/proposta comercial do robô (página estática para divulgação)."""
    return send_from_directory(os.path.join(BASE, "templates"), "manual.html")


@app.get("/implantacao")
def implantacao():
    """Guia de implantação do robô para novas franquias (página estática)."""
    return send_from_directory(os.path.join(BASE, "templates"), "implantacao.html")


@app.get("/cadastro")
def cadastro():
    """Formulário que a franquia preenche com os dados da unidade (página estática)."""
    return send_from_directory(os.path.join(BASE, "templates"), "cadastro.html")


# ============ cache "stale-while-revalidate" da grade de horários ============
# Calcular a grade faz DEZENAS de chamadas ao EVO (lento na carga fria). Para o
# formulário nunca travar em "Carregando horários...", servimos a última grade
# do cache NA HORA e recalculamos em segundo plano quando ela envelhece. As
# REGRAS não mudam (o cálculo é o mesmo); e o /api/book revalida o horário ao
# vivo no EVO na hora de marcar, então mostrar uma grade com poucos segundos de
# idade é seguro (vaga/experimentais/ocupação continuam valendo no agendamento).
FORM_SLOTS_TTL = float(os.getenv("FORM_SLOTS_TTL", "120"))  # segundos de "frescor"
# A grade é carregada EM PARTES (4 dias, depois +4, depois +2 — até FORM_DAYS).
# Motivo: cada horário disponível custa 1 consulta ao EVO e a cota é 40/min;
# pedir os 10 dias de uma vez passava de 40 chamadas, o EVO freava e a request
# estourava o timeout de 120s do gunicorn ("Falha de conexão"). Em partes, cada
# pedido fica bem abaixo do limite e responde em segundos.
FORM_FIRST_DAYS = int(os.getenv("FORM_FIRST_DAYS", "4"))
# Um cache por tamanho de janela: {days: {"exp":..., "data":..., ...}}
_slots_cache_por_dias = {}
_slots_cache = {"exp": 0.0, "data": None, "refreshing": False, "error": None}
_slots_cache_lock = threading.Lock()


def _cache_de(days):
    c = _slots_cache_por_dias.get(days)
    if c is None:
        c = {"exp": 0.0, "data": None, "refreshing": False, "error": None}
        _slots_cache_por_dias[days] = c
    return c


# Só UM cálculo da grade por vez no processo inteiro. Sem isso, cada visitante
# dispara dezenas de chamadas ao EVO em paralelo e estoura o limite de 40
# req/min (HTTP 429) — foi o que aconteceu com o anúncio trazendo várias
# pessoas ao mesmo tempo. Quem chegar durante um cálculo espera e aproveita o
# resultado dele, em vez de abrir outro.
_compute_lock = threading.Lock()


def _compute_slots(days):
    # use_cache=True de propósito: o cache interno do orchestrator é a segunda
    # trava contra o 429 (não desligar).
    # Timeout do EVO mais curto SÓ no formulário: uma chamada travada falha em
    # ~15s (em vez de segurar 30s), então a grade degrada rápido em vez de
    # empurrar o cálculo para além do timeout do gunicorn. FORM_EVO_TIMEOUT=0
    # usa o padrão global (EVO_TIMEOUT).
    _to = int(os.getenv("FORM_EVO_TIMEOUT", "15") or 0) or None
    return available_slots(days=days, max_ocupacao=FORM_MAX_OCUPACAO,
                           evo=EvoClient(timeout=_to))


def _refresh_slots_bg(days):
    """Dispara UM recálculo em segundo plano (ignora se já houver um rodando)."""
    c = _cache_de(days)
    with _slots_cache_lock:
        if c["refreshing"]:
            return
        c["refreshing"] = True

    def _worker():
        try:
            with _compute_lock:           # nunca em paralelo com outro cálculo
                data = _compute_slots(days)
            c["data"] = data
            c["exp"] = time.monotonic() + FORM_SLOTS_TTL
            c["error"] = None
        except Exception as e:
            # Guarda o erro para /api/slots poder mostrar (senão fica "vazio" mudo).
            c["error"] = f"{type(e).__name__}: {e}"
            app.logger.exception("Falha ao recalcular a grade (background)")
        finally:
            c["refreshing"] = False

    threading.Thread(target=_worker, name=f"slots-refresh-{days}", daemon=True).start()


def _get_slots_cached(days):
    """NUNCA bloqueia a request. O cálculo da grade faz dezenas de chamadas ao
    EVO e pode levar 1-2 min quando o EVO está lento — se fizermos isso enquanto
    a aluna espera, a página fica "Carregando horários..." por minutos e o worker
    do gunicorn pode ser morto (WORKER TIMEOUT). Então:
      - grade fresca  → devolve na hora;
      - grade velha   → devolve a velha e recalcula ao fundo;
      - grade fria    → dispara o cálculo ao fundo e devolve None (a página mostra
                        "preparando..." e tenta de novo em segundos).
    O aquecedor abaixo mantém a 1ª janela quase sempre quente, então na prática a
    aluna vê os horários na hora."""
    c = _cache_de(days)
    data = c["data"]
    if data is not None and c["exp"] > time.monotonic():
        return data                       # fresco → instantâneo
    _refresh_slots_bg(days)               # velho ou frio → recalcula ao fundo
    return data                           # devolve o que tem (pode ser None = aquecendo)


# Aquece a 1ª janela já no boot (em segundo plano; não bloqueia o 1º request).
_refresh_slots_bg(min(FORM_FIRST_DAYS, FORM_DAYS))


def _warmer_loop():
    """Mantém a grade da 1ª janela sempre quente, para a aluna quase nunca pegar
    o cálculo frio. Recalcula um pouco antes de o cache expirar."""
    dias_warm = min(FORM_FIRST_DAYS, FORM_DAYS)
    intervalo = max(30.0, FORM_SLOTS_TTL - 20)
    while True:
        try:
            # Se o VPS está enviando a grade pronta, a Render NÃO calcula nada
            # (evita competir pela cota de 40/min do EVO com o VPS). Só aquece
            # localmente como reserva quando não há grade enviada fresca.
            if not _pushed_fresh():
                _refresh_slots_bg(dias_warm)
        except Exception:
            app.logger.exception("Aquecedor da grade falhou")
        time.sleep(intervalo)


if os.getenv("FORM_WARMER", "1") not in ("0", "false", "False"):
    threading.Thread(target=_warmer_loop, name="slots-warmer", daemon=True).start()


# ============ grade enviada pelo VPS (VPS-push) — via instantânea ============
# Calcular a grade faz 1 chamada /detail por horário: rápido no VPS (sempre
# ligado e perto do EVO), lento na Render free. Então o VPS calcula a grade
# pronta e ENVIA para cá (POST /api/slots/push); o /api/slots serve essa grade
# NA HORA, sem tocar no EVO. Se a grade enviada envelhecer (o VPS parou de
# enviar), caímos automaticamente no cálculo local (warming) como reserva.
SLOTS_PUSH_TOKEN = os.getenv("FORM_SLOTS_TOKEN", "")
SLOTS_PUSH_TTL = float(os.getenv("FORM_PUSH_TTL", "1800"))   # 30 min de validade
_PUSH_FILE = os.getenv("FORM_PUSH_FILE", "slots_pushed.json")
_pushed = {"ts": 0.0, "slots": None}


def _pushed_load():
    try:
        with open(_PUSH_FILE, encoding="utf-8") as f:
            d = json.load(f)
        _pushed["slots"] = d.get("slots")
        _pushed["ts"] = float(d.get("ts") or 0)
    except (OSError, ValueError):
        pass


_pushed_load()


def _pushed_fresh():
    return (_pushed["slots"] is not None and SLOTS_PUSH_TTL > 0
            and (time.time() - _pushed["ts"]) < SLOTS_PUSH_TTL)


def _pushed_slice(days):
    """Fatia a grade enviada (janela cheia) para os próximos `days` dias e tira
    horários que já passaram."""
    corte = (datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
             + timedelta(days=days)).strftime("%Y-%m-%d")
    agora = datetime.now().strftime("%Y-%m-%d %H:%M")
    return [s for s in (_pushed["slots"] or [])
            if s.get("activityDate", "") >= agora and s.get("date", "") < corte]


def _agrupa_por_dia(slots):
    dias = {}
    for s in slots:
        dias.setdefault(s["date"], []).append({
            "idConfiguration": s["idConfiguration"],
            "activityDate": s["activityDate"],
            "time": s["time"],
            "activity": s["activity"],
            "disponivel": s["disponivel"],
            "freeSpots": s["freeSpots"],
        })
    return dias


@app.post("/api/slots/push")
def api_slots_push():
    """O VPS envia a grade pronta para cá. Protegido por FORM_SLOTS_TOKEN."""
    if not SLOTS_PUSH_TOKEN or request.args.get("token") != SLOTS_PUSH_TOKEN:
        return jsonify({"ok": False, "erro": "não autorizado"}), 401
    body = request.get_json(silent=True) or {}
    slots = body.get("slots")
    if not isinstance(slots, list):
        return jsonify({"ok": False, "erro": "campo 'slots' (lista) obrigatório"}), 400
    _pushed["slots"] = slots
    _pushed["ts"] = time.time()
    try:
        with open(_PUSH_FILE, "w", encoding="utf-8") as f:
            json.dump({"ts": _pushed["ts"], "slots": slots}, f, ensure_ascii=False)
    except OSError:
        app.logger.exception("Falha ao gravar a grade enviada")
    return jsonify({"ok": True, "recebidos": len(slots), "ts": _pushed["ts"]})


@app.get("/api/diag-evo")
def api_diag_evo():
    """Cronometra UMA chamada ao EVO (list_schedule) para medir a latência real
    do servidor até a API do EVO. Só diagnóstico — não altera nada."""
    from datetime import datetime as _dt
    t0 = time.monotonic()
    try:
        raw = EvoClient().list_schedule(_dt.now(), show_full_week=True) or []
        dt_ms = int((time.monotonic() - t0) * 1000)
        ids = {}
        for s in raw[:400]:
            k = f"{s.get('idActivity')}|{s.get('name')}"
            ids[k] = ids.get(k, 0) + 1
        return jsonify({"ok": True, "ms": dt_ms, "turmas": len(raw), "atividades": ids})
    except Exception as e:
        return jsonify({"ok": False, "ms": int((time.monotonic() - t0) * 1000),
                        "erro": f"{type(e).__name__}: {e}"}), 502


@app.get("/api/slots")
def api_slots():
    """Grade agrupada por dia, com flag de disponibilidade.

    ?days=N → carrega só os próximos N dias (padrão FORM_FIRST_DAYS). A tela
    pede 4, depois 8, depois 10, para cada pedido caber na cota do EVO."""
    try:
        days = int(request.args.get("days") or FORM_FIRST_DAYS)
    except (TypeError, ValueError):
        days = FORM_FIRST_DAYS
    days = max(1, min(days, FORM_DAYS))     # nunca além da janela configurada

    # 1) Prioridade: grade enviada pelo VPS (instantânea, sem tocar no EVO).
    if _pushed_fresh():
        dias = _agrupa_por_dia(_pushed_slice(days))
        return jsonify({"ok": True, "dias": dias, "maxOcupacao": FORM_MAX_OCUPACAO,
                        "days": days, "maxDays": FORM_DAYS,
                        "temMais": days < FORM_DAYS, "fonte": "vps"})

    # 2) Reserva: cálculo local (com "warming") se o VPS não estiver enviando.
    try:
        slots = _get_slots_cached(days)
    except Exception:
        app.logger.exception("Falha ao listar grade")
        slots = _cache_de(days).get("data")

    if slots is None:
        # Grade ainda "aquecendo" (o cálculo roda em segundo plano). Responde JÁ,
        # sem prender a request; a página tenta de novo em instantes. Isso evita
        # o "Carregando..." infinito e o WORKER TIMEOUT.
        c = _cache_de(days)
        return jsonify({"ok": True, "dias": {}, "warming": True,
                        "days": days, "maxDays": FORM_DAYS,
                        "temMais": days < FORM_DAYS,
                        "_debug": {"error": c.get("error"), "refreshing": c.get("refreshing")}})

    dias = _agrupa_por_dia(slots)
    resp = {"ok": True, "dias": dias, "maxOcupacao": FORM_MAX_OCUPACAO,
            "days": days, "maxDays": FORM_DAYS, "temMais": days < FORM_DAYS}
    # Se veio vazio, expõe o motivo (erro do cálculo ou ainda aquecendo) para
    # diagnóstico — assim o /api/slots deixa de ser "vazio mudo".
    if not dias:
        c = _cache_de(days)
        resp["_debug"] = {
            "error": c.get("error"),
            "refreshing": c.get("refreshing"),
            "has_data": c.get("data") is not None,
        }
    return jsonify(resp)


@app.post("/api/book")
def api_book():
    dados = request.get_json(silent=True) or {}
    erros, limpo = _valida(dados)
    if erros:
        return jsonify({"ok": False, "erros": erros}), 400

    id_config = dados.get("idConfiguration")
    activity_date = dados.get("activityDate")   # "yyyy-MM-dd HH:mm"

    evo = EvoClient()

    # limite: 1 aula experimental por pessoa. Se já foi vendido o serviço da
    # experimental para esse e-mail, telefone OU CPF, bloqueia (sem cadastrar nem vender).
    try:
        idp = evo.find_prospect_id(email=limpo["email"], phone=limpo["telefone"], document=limpo["cpf"])
        if idp and _ja_tem_experimental(evo, idp):
            return jsonify({"ok": False, "bloqueio": "ja_tem_experimental", "erro": BLOCK_MSG}), 409
    except Exception:
        app.logger.exception("Falha ao checar experimental existente (libero o agendamento)")

    # revalida a vaga no momento do envio (regra do formulário: ocupation <= 7)
    try:
        slots = available_slots(evo=evo, days=FORM_DAYS, max_ocupacao=FORM_MAX_OCUPACAO)
    except Exception as e:
        app.logger.exception("Falha ao revalidar grade")
        return jsonify({"ok": False, "erro": f"Erro ao consultar a agenda: {e}"}), 502

    escolha = next((s for s in slots
                    if str(s["idConfiguration"]) == str(id_config)
                    and s["activityDate"] == activity_date), None)
    if not escolha:
        return jsonify({"ok": False, "erro": "Esse horário não está mais na grade. Atualize e escolha outro."}), 409
    if not escolha["disponivel"]:
        return jsonify({"ok": False, "erro": "Esse horário acabou de lotar. Escolha outro, por favor."}), 409

    # cadastro + venda + matrícula no EVO
    try:
        res = book_experimental(
            name=limpo["nome"], when=activity_date, email=limpo["email"],
            phone=limpo["telefone"], document=limpo["cpf"], birthday=limpo["nascimento"],
            evo=evo,
        )
    except TurmaLotadaError:
        return jsonify({"ok": False, "erro": "Esse horário acabou de lotar. Escolha outro, por favor."}), 409
    except Exception as e:
        app.logger.exception("Falha no agendamento")
        return jsonify({"ok": False, "erro": f"Não consegui concluir o agendamento: {e}"}), 500

    # enfileira a confirmação p/ o PC enviar pelo WhatsApp do Studio
    try:
        msg = _confirm_message(limpo["nome"], res.when)
        _outbox_append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "contactId": "form-" + limpo["cpf"],
            "name": limpo["nome"], "phone": br_phone_with_9(limpo["telefone"]),
            "when": res.when, "message": msg, "status": "pending",
        })
    except Exception:
        app.logger.exception("Agendou mas falhou ao enfileirar a confirmação")

    try:
        _ind_append("agendou", (request.get_json(silent=True) or {}).get("origem", ""),
                    {"hora": _hora_de(res.when)})
    except Exception:
        pass
    return jsonify({"ok": True, "when": res.when, "idProspect": res.id_prospect,
                    "activity": res.activity})


@app.get("/api/ind/pending")
def api_ind_pending():
    if not OUTBOX_TOKEN or request.args.get("token") != OUTBOX_TOKEN:
        return jsonify({"ok": False, "erro": "nao autorizado"}), 401
    return jsonify({"ok": True, "eventos": _ind_read_all()})


@app.post("/api/ind/ack")
def api_ind_ack():
    if not OUTBOX_TOKEN or request.args.get("token") != OUTBOX_TOKEN:
        return jsonify({"ok": False, "erro": "nao autorizado"}), 401
    ids = (request.get_json(silent=True) or {}).get("ids", [])
    _ind_remove(ids)
    return jsonify({"ok": True, "removidos": len(ids)})


@app.get("/api/outbox/pending")
def api_outbox_pending():
    if not OUTBOX_TOKEN or request.args.get("token") != OUTBOX_TOKEN:
        return jsonify({"ok": False, "erro": "não autorizado"}), 401
    pend = [r for r in _outbox_read_all() if r.get("status") == "pending"]
    return jsonify({"ok": True, "rows": pend})


@app.post("/api/outbox/ack")
def api_outbox_ack():
    if not OUTBOX_TOKEN or request.args.get("token") != OUTBOX_TOKEN:
        return jsonify({"ok": False, "erro": "não autorizado"}), 401
    keys = (request.get_json(silent=True) or {}).get("keys") or []
    _outbox_ack(keys)
    return jsonify({"ok": True, "acked": len(keys)})


@app.get("/health")
def health():
    return jsonify({"ok": True})

@app.post("/api/book-sofia")
def api_book_sofia():
    # 1) Autenticação simples por token compartilhado (só a Sofia conhece).
    if not SOFIA_TOKEN or request.headers.get("X-Sofia-Token") != SOFIA_TOKEN:
        return jsonify({"ok": False, "erro": "não autorizado"}), 401

    dados = request.get_json(silent=True) or {}
    nome = (dados.get("nome") or "").strip()
    email = (dados.get("email") or "").strip().lower()
    telefone = only_digits(dados.get("telefone"))
    when = (dados.get("when") or "").strip()   # "quinta-feira às 16:30" ou "2026-07-30 16:30"

    # 2) Validação mínima (sem CPF/nascimento — fluxo leve do WhatsApp).
    if len(nome.split()) < 2:
        return jsonify({"ok": False, "erro": "nome incompleto"}), 400
    if not email or "@" not in email:
        return jsonify({"ok": False, "erro": "email inválido"}), 400
    if not when:
        return jsonify({"ok": False, "erro": "horário não informado"}), 400

    # 3) Agenda no EVO reusando TODA a sua lógica (cadastro + venda + matrícula,
    #    deduplicação, limite de experimentais, etc.). CPF/nascimento ficam de fora.
    try:
        res = book_experimental(name=nome, when=when, email=email, phone=telefone)
    except TurmaLotadaError as e:
        # Turma cheia / inexistente / fora de janela: o lead JÁ foi cadastrado no EVO.
        # Devolvemos as alternativas para a Sofia oferecer outro horário à aluna.
        return jsonify({
            "ok": False,
            "motivo": "sem_vaga",
            "detalhe": str(e),
            "alternativas": [a.get("when") for a in (e.alternatives or [])][:5],
        }), 409
    except Exception as e:
        app.logger.exception("Sofia: falha no agendamento")
        return jsonify({"ok": False, "erro": f"não consegui agendar: {e}"}), 500

    # 4) Enfileira a confirmação na OUTBOX — o bot do Studio (8550-8065) vai ler
    #    essa fila e enviar a mensagem para a aluna, igual ao fluxo do formulário.
    try:
        msg = _confirm_message(nome, res.when)
        _outbox_append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "contactId": "sofia-" + (telefone or ""),
            "name": nome, "phone": br_phone_with_9(telefone),
            "when": res.when, "message": msg, "status": "pending",
        })
    except Exception:
        app.logger.exception("Sofia: agendou mas falhou ao enfileirar a confirmação")

    # 5) Sucesso: a aula foi agendada no EVO.
    return jsonify({
        "ok": True,
        "when": res.when,               # "2026-07-30 16:30" (data real resolvida)
        "idProspect": res.id_prospect,
        "activity": res.activity,
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=True)
