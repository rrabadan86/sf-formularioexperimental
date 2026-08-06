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
from datetime import datetime

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
    return send_from_directory(os.path.join(BASE, "templates"), "index.html")


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
    return available_slots(days=days, max_ocupacao=FORM_MAX_OCUPACAO)


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
    c = _cache_de(days)
    data = c["data"]
    if data is not None and c["exp"] > time.monotonic():
        return data                       # fresco → instantâneo
    if data is not None:
        _refresh_slots_bg(days)           # velho → serve o velho e recalcula ao fundo
        return data
    # SEM grade pronta: calcula AQUI e espera terminar. NUNCA devolvemos vazio
    # por impaciência — melhor demorar um pouco do que dizer à aluna que não há
    # horário. Só a 1ª visita paga a espera; as próximas usam o cache.
    with _compute_lock:                   # um cálculo por vez (evita o 429 do EVO)
        pronta = c["data"]                # outro request pode ter calculado enquanto esperávamos
        if pronta is not None and c["exp"] > time.monotonic():
            return pronta
        data = _compute_slots(days)
        c["data"] = data
        c["exp"] = time.monotonic() + FORM_SLOTS_TTL
        c["error"] = None
        return data


# Aquece a 1ª janela já no boot (em segundo plano; não bloqueia o 1º request).
_refresh_slots_bg(min(FORM_FIRST_DAYS, FORM_DAYS))


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

    try:
        slots = _get_slots_cached(days)
    except Exception as e:
        app.logger.exception("Falha ao listar grade")
        # Se o EVO recusou agora (ex.: 429 por excesso de chamadas), mas temos a
        # última grade boa, mostramos ela em vez de assustar a aluna com erro.
        antiga = _cache_de(days).get("data")
        if antiga:
            slots = antiga
        else:
            return jsonify({"ok": False, "erro": f"Não consegui carregar a grade: {e}"}), 502
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
    # experimental para esse e-mail/telefone, bloqueia (sem cadastrar nem vender).
    try:
        idp = evo.find_prospect_id(email=limpo["email"], phone=limpo["telefone"])
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

    return jsonify({"ok": True, "when": res.when, "idProspect": res.id_prospect,
                    "activity": res.activity})


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
