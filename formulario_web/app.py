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


# =============================== validações ==================================
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _valida(dados):
    erros = {}
    nome = (dados.get("nome") or "").strip()
    if len(nome.split()) < 2:
        erros["nome"] = "Informe o nome completo."
    cpf = only_digits(dados.get("cpf"))
    if len(cpf) != 11:
        erros["cpf"] = "CPF deve ter 11 dígitos."
    tel = only_digits(dados.get("telefone"))
    if len(tel) < 10:
        erros["telefone"] = "Telefone inválido."
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


@app.get("/api/slots")
def api_slots():
    """Grade dos próximos FORM_DAYS dias, agrupada por dia, com flag de disponibilidade."""
    try:
        slots = available_slots(days=FORM_DAYS, max_ocupacao=FORM_MAX_OCUPACAO)
    except Exception as e:
        app.logger.exception("Falha ao listar grade")
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
    return jsonify({"ok": True, "dias": dias, "maxOcupacao": FORM_MAX_OCUPACAO})


@app.post("/api/book")
def api_book():
    dados = request.get_json(silent=True) or {}
    erros, limpo = _valida(dados)
    if erros:
        return jsonify({"ok": False, "erros": erros}), 400

    id_config = dados.get("idConfiguration")
    activity_date = dados.get("activityDate")   # "yyyy-MM-dd HH:mm"

    # revalida a vaga no momento do envio (regra do formulário: ocupation <= 7)
    try:
        evo = EvoClient()
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=True)
