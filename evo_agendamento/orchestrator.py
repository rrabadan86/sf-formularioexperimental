# Orquestração: dados da aluna -> cadastro + venda + matrícula (agendamento) no EVO.
#
# Espelha o processo manual do Studio:
#   1) cadastra a oportunidade (prospect)      -> POST /prospects
#   2) vende o serviço "Aula Experimental"     -> POST /sales
#   3) agenda normal (matricula na turma)      -> POST /activities/schedule/enroll
# A checagem de vaga usa a capacidade da turma (ocupation < capacity, ex.: 9),
# sem depender do flag allowExperimentalClass.
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from . import config
from .evo_client import EvoClient, EvoError
from .util import (
    br_phone_with_9,
    fmt_datetime_evo, friendly_when_ptbr, only_digits, parse_when, same_slot,
    session_free_general, session_has_room_normal, session_start_datetime, split_name,
)

log = logging.getLogger("orquestrador")

# Trechos de mensagem do EVO que indicam turma sem vaga (fonte da verdade é o EVO).
_LOTADA_HINTS = ("lotad", "capacidad", "cheia", "esgotad", "sem vaga", "vagas", "limite")

# Trechos que indicam que o EVO recusou a matrícula por a turma estar FORA DA JANELA
# de agendamento (antecedência maior que a permitida). Não é falta de vaga: cadastro e
# venda já foram feitos; falta a recepção matricular quando a janela abrir.
_FORA_JANELA_HINTS = ("out of booking hours", "booking hours", "fora da janela",
                      "fora do horario de agendamento", "anteceden")

# Trechos que indicam que o prospect JÁ ESTÁ matriculado nessa turma (uma tentativa
# anterior já matriculou). Tratamos como agendado: marca "Feito" e não revende.
_JA_MATRICULADO_HINTS = ("already booked", "already enrolled", "já matriculad",
                         "ja matriculad", "já inscrit", "ja inscrit")


@dataclass
class BookingResult:
    id_prospect: int
    prospect_created: bool
    id_configuration: Optional[int]
    when: str
    activity: str
    service: str
    sold: bool
    enrolled: bool


class TurmaLotadaError(RuntimeError):
    """Não foi possível agendar no horário escolhido (turma cheia ou inexistente).
    Traz horários alternativos com vaga para a recepção/IA sugerir."""

    def __init__(self, when, alternatives=None, reason="sem vaga"):
        self.when = when
        self.reason = reason
        self.alternatives = alternatives or []
        msg = f"Turma de {when}: {reason}."
        if self.alternatives:
            msg += " Horários com vaga: " + ", ".join(a["when"] for a in self.alternatives[:5])
        super().__init__(msg)


def _match_activity(sess, activity=None, id_activity=None) -> bool:
    if id_activity and sess.get("idActivity") != int(id_activity):
        return False
    if activity and activity.lower() not in (sess.get("name") or "").lower():
        return False
    return True


def _find_session(schedule, when, activity=None, id_activity=None):
    """Acha a turma cujo horário (data + HH:MM) bate com o escolhido."""
    for s in schedule:
        if same_slot(s, when) and _match_activity(s, activity, id_activity):
            return s
    return None


def list_alternatives(evo, when, activity=None, id_activity=None, branch_id=None, limit=5):
    """Turmas futuras da mesma atividade que ainda têm vaga (por capacidade)."""
    schedule = evo.list_schedule(when, show_full_week=True, branch_id=branch_id)
    out = []
    for s in schedule:
        if not session_has_room_normal(s) or not _match_activity(s, activity, id_activity):
            continue
        start = session_start_datetime(s)
        if start is None:
            continue
        out.append({
            "when": start.strftime("%Y-%m-%d %H:%M"),
            "activity": s.get("name"),
            "idConfiguration": s.get("idConfiguration"),
            "freeSpots": session_free_general(s),
            "_start": start,
        })
    out.sort(key=lambda a: a["_start"])
    for a in out:
        a.pop("_start", None)
    return out[:limit]


def notify_studio_full(zee, name, phone, when, alternatives=None, reason="sem vaga",
                       activity=None, studio_phone=None):
    """Avisa o WhatsApp do Studio (pela ZEE, 996847251 -> Studio) que a aluna tentou
    agendar e não deu (turma lotada ou inexistente). Retorna True se enviou."""
    studio_phone = studio_phone or config.ZEE_STUDIO_PHONE
    if not studio_phone:
        log.warning("Falha de agendamento, mas ZEE_STUDIO_PHONE não configurado: aviso não enviado.")
        return False
    try:
        turma = friendly_when_ptbr(when)
    except (ValueError, TypeError):
        turma = str(when)
    if activity:
        turma = f"{activity} · {turma}"
    alt_txt = ""
    if alternatives:
        opcoes = "; ".join(f"{a['when']} ({a.get('freeSpots')} vaga(s))" for a in alternatives[:5])
        alt_txt = f"\nHorários com vaga: {opcoes}"
    reason_l = (reason or "").lower()
    if "experimenta" in reason_l:
        template = config.ZEE_ALERT_EXPERIMENTAIS
    elif any(k in reason_l for k in ("janela", "booking hours", "anteceden")):
        template = config.ZEE_ALERT_FORA_JANELA
    elif any(k in reason_l for k in ("não há turma", "nao ha turma", "inexist")):
        template = config.ZEE_ALERT_INEXISTENTE
    else:
        template = config.ZEE_ALERT_LOTADA
    msg = template.format(name=name or "(sem nome)", phone=phone or "(sem telefone)",
                          turma=turma, alternatives=alt_txt)
    zee.notify_phone(studio_phone, msg, name=config.ZEE_STUDIO_NAME)
    log.info("Studio avisado (%s): %s tentou %s (%s)", studio_phone, name, turma, reason)
    return True


def _enrollment_experimental_ativa(e):
    """True se o enrollment é uma aula EXPERIMENTAL que ocupa vaga (ativa).
    Experimental = tem idProspect e não tem idMember. Ativa = não cancelada
    (status 2 = cancelada) nem removida/suspensa."""
    if not e.get("idProspect") or e.get("idMember"):
        return False
    if e.get("status") == 2 or e.get("removed") or e.get("suspended"):
        return False
    return True


def count_experimentais(evo, id_configuration, when, branch_id=None):
    """Quantas aulas experimentais ATIVAS já existem na turma (idConfiguration) no
    dia `when`. Usa GET /activities/schedule/detail (campo `enrollments`).
    Retorna int, ou None se não conseguir consultar (aí não bloqueia)."""
    try:
        detail = evo.schedule_detail(id_configuration=id_configuration,
                                     activity_date=when, branch_id=branch_id) or {}
    except EvoError as e:
        log.warning("Não consegui checar experimentais da turma %s (%s): %s",
                    id_configuration, when, e)
        return None
    enrollments = detail.get("enrollments") or []
    return sum(1 for e in enrollments if _enrollment_experimental_ativa(e))


def book_experimental(
    name: str,
    when,                       # datetime ou string (horário escolhido)
    email: str = None,
    phone: str = None,
    activity: str = None,       # nome da atividade (default: EVO_ACTIVITY)
    service: str = None,        # nome do serviço da aula experimental (default: EVO_SERVICE)
    id_activity=None,           # ou por id (default: EVO_ACTIVITY_ID)
    id_service=None,            # ou por id (default: EVO_SERVICE_ID)
    branch_id=None,
    check_capacity: bool = True,   # checa vaga (ocupation < capacity) e sugere alternativas
    sell_service: bool = True,     # vende o serviço "Aula Experimental" antes de matricular
    document: str = None,          # CPF (opcional, usado pelo formulário web)
    birthday: str = None,          # data de nascimento yyyy-MM-dd (opcional)
    evo: EvoClient = None,
) -> BookingResult:
    """Fluxo real do Studio: cadastro -> venda do serviço -> matrícula na turma.

    Se a turma do horário estiver cheia (ou não existir turma nesse horário),
    levanta TurmaLotadaError com horários alternativos que têm vaga.
    """
    evo = evo or EvoClient()

    # "segunda às 8h15" / "amanhã 07:00" / "dia 17 às 8h" -> data real (yyyy-MM-dd HH:mm)
    when = parse_when(when)

    activity = activity or config.EVO_ACTIVITY or None
    service = service or config.EVO_SERVICE or None
    id_activity = id_activity or (config.EVO_ACTIVITY_ID or None)
    id_service = id_service or (config.EVO_SERVICE_ID or None)

    # Para vender, precisamos do id do serviço (resolve pelo nome se só veio o nome).
    if sell_service and not id_service:
        if service:
            match = next((s for s in evo.list_services(branch_id=branch_id)
                          if service.lower() in (s.get("nameService") or "").lower()), None)
            id_service = match.get("idService") if match else None
        if not id_service:
            raise ValueError("Informe o serviço da aula experimental (EVO_SERVICE_ID ou EVO_SERVICE).")

    # 1) cadastro (idempotente) — registra a OPORTUNIDADE primeiro, antes de
    #    checar a turma. Assim o lead sempre fica cadastrado no EVO, mesmo que a
    #    turma esteja lotada (nesse caso não vende nem matricula — ver passo 2).
    first, last = split_name(name)
    id_prospect, created = evo.get_or_create_prospect(
        name=first, last_name=last, email=email, phone=phone, branch_id=branch_id,
        document=document, birthday=birthday,
    )

    # 2) localizar a turma do horário e checar vaga (cadastro JÁ feito acima).
    #    Se a turma estiver cheia ou não existir nesse horário, a oportunidade já
    #    ficou cadastrada, mas NÃO vende nem matricula — avisa o Studio p/ tratar.
    schedule = evo.list_schedule(when, show_full_week=True, branch_id=branch_id)
    session = _find_session(schedule, when, activity, id_activity)
    if session is None:
        raise TurmaLotadaError(
            fmt_datetime_evo(when),
            alternatives=list_alternatives(evo, when, activity, id_activity, branch_id),
            reason="não há turma nesse horário",
        )
    if check_capacity and not session_has_room_normal(session):
        raise TurmaLotadaError(
            fmt_datetime_evo(when),
            alternatives=list_alternatives(evo, when, activity, id_activity, branch_id),
        )

    id_configuration = session.get("idConfiguration")

    # 2b) limite de aulas experimentais por horário (ex.: máx. 2). Conta as
    #     experimentais ATIVAS já marcadas na turma; se atingiu o limite, trata
    #     como turma cheia — a oportunidade já ficou cadastrada, mas NÃO vende nem
    #     matricula, e o Studio é avisado para reagendar.
    if check_capacity and config.EVO_MAX_EXPERIMENTAIS:
        n_exp = count_experimentais(evo, id_configuration, when, branch_id=branch_id)
        if n_exp is not None and n_exp >= config.EVO_MAX_EXPERIMENTAIS:
            raise TurmaLotadaError(
                fmt_datetime_evo(when),
                alternatives=list_alternatives(evo, when, activity, id_activity, branch_id),
                reason=f"já há {n_exp} aulas experimentais nesse horário",
            )

    # 3) vende o serviço "Aula Experimental"
    sold = False
    if sell_service and id_service:
        evo.create_sale(
            id_service=id_service, id_prospect=id_prospect,
            service_value=config.EVO_SERVICE_VALUE, payment=config.EVO_PAYMENT,
            branch_id=branch_id,
        )
        sold = True

    # 4) matricula na turma (agendamento normal). O EVO é a fonte da verdade da
    # capacidade: se a última vaga sumiu, ele recusa e devolvemos alternativas.
    try:
        evo.enroll_schedule(id_configuration, activity_date=when, id_prospect=id_prospect)
    except EvoError as e:
        emsg = str(e).lower()
        if check_capacity and any(h in emsg for h in _LOTADA_HINTS):
            raise TurmaLotadaError(
                fmt_datetime_evo(when),
                alternatives=list_alternatives(evo, when, activity, id_activity, branch_id),
            ) from e
        if any(h in emsg for h in _FORA_JANELA_HINTS):
            # Turma existe e tem vaga, mas o EVO não deixa agendar com essa antecedência.
            # Cadastro + venda já foram feitos: avisamos o Studio p/ matrícula manual e
            # marcamos como resolvido (senão o job revende o serviço a cada ciclo).
            raise TurmaLotadaError(
                fmt_datetime_evo(when),
                reason="fora da janela de agendamento",
            ) from e
        if any(h in emsg for h in _JA_MATRICULADO_HINTS):
            # A aluna JÁ está matriculada nessa turma (tentativa anterior matriculou).
            # Considero agendado: cai no return abaixo -> o job marca "Feito" e para de
            # reprocessar/revender. NÃO relança o erro.
            log.info("Prospect %s já estava matriculado na turma — tratando como agendado.",
                     id_prospect)
        else:
            raise

    return BookingResult(
        id_prospect=id_prospect,
        prospect_created=created,
        id_configuration=id_configuration,
        when=fmt_datetime_evo(when),
        activity=session.get("name") or activity or f"id={id_activity}",
        service=service or f"id={id_service}",
        sold=sold,
        enrolled=True,
    )


def available_slots(evo=None, days=10, activity=None, id_activity=None, branch_id=None,
                    max_ocupacao=7, now=None):
    """Grade da aula experimental nos próximos `days` dias (padrão 10).
    Cada item traz vagas e um flag `disponivel`: True quando ocupation <= max_ocupacao
    (padrão 7). Turmas com 8/9 aparecem, mas com disponivel=False (não deixa marcar).
    Só retorna horários do agora pra frente."""
    evo = evo or EvoClient()
    activity = activity or config.EVO_ACTIVITY or None
    id_activity = id_activity or (config.EVO_ACTIVITY_ID or None)
    now = now or datetime.now()
    inicio = now.replace(hour=0, minute=0, second=0, microsecond=0)
    fim = inicio + timedelta(days=days)          # exclusivo

    vistos, itens = set(), []
    d = inicio
    while d < fim:                                # cobre as semanas do intervalo
        for s in (evo.list_schedule(d, show_full_week=True, branch_id=branch_id) or []):
            if not _match_activity(s, activity, id_activity):
                continue
            dt = session_start_datetime(s)
            if dt is None or dt < now or dt >= fim:
                continue
            key = (s.get("idConfiguration"), dt.isoformat())
            if key in vistos:
                continue
            vistos.add(key)
            cap = s.get("capacity")
            ocup = s.get("ocupation") or 0
            disponivel = (ocup <= max_ocupacao)
            n_exp = None
            # Só checa experimentais nas turmas que, de outra forma, estariam
            # disponíveis (evita uma chamada /detail por turma já cheia).
            if disponivel and config.EVO_MAX_EXPERIMENTAIS:
                n_exp = count_experimentais(evo, s.get("idConfiguration"), dt, branch_id=branch_id)
                if n_exp is not None and n_exp >= config.EVO_MAX_EXPERIMENTAIS:
                    disponivel = False       # já tem 2 experimentais -> indisponível
            itens.append({
                "idConfiguration": s.get("idConfiguration"),
                "activityDate": fmt_datetime_evo(dt),   # "yyyy-MM-dd HH:mm"
                "date": dt.strftime("%Y-%m-%d"),
                "time": dt.strftime("%H:%M"),
                "weekday": dt.weekday(),                 # 0=segunda ... 6=domingo
                "activity": s.get("name") or activity or "",
                "capacity": cap,
                "ocupation": ocup,
                "freeSpots": (max(cap - ocup, 0) if cap is not None else None),
                "experimentais": n_exp,                  # quantas experimentais ativas (ou None)
                "disponivel": disponivel,
            })
        d += timedelta(days=7)
    itens.sort(key=lambda x: x["activityDate"])
    return itens


def booking_data_from_contact(contact: dict, summary: str = None):
    """Extrai (nome, telefone, e-mail, horário) de um contato do ZEE.

    Fonte principal: o RESUMO do ZEE configurado para devolver um JSON com
    nome_completo / email / dia / hora (ver instrução no README). Telefone vem
    do contato. Se o resumo não vier em JSON, cai no fallback (metadata / regex)."""
    from .util import extract_email, parse_summary_json

    contact = contact or {}
    phone = contact.get("phone")

    # 1) resumo em JSON (caminho recomendado)
    s = parse_summary_json(summary)
    if s:
        name = (s.get("nome_completo") or s.get("nome") or "").strip() or None
        email = (s.get("email") or "").strip() or None
        dia = (s.get("dia") or "").strip()
        hora = (s.get("hora") or "").strip()
        when = f"{dia} {hora}".strip() or None
        return {"name": name, "phone": phone, "email": email, "when": when}

    # 2) fallback: metadata do contato / e-mail no texto
    meta = contact.get("metadata") or {}
    name = meta.get("nome") or contact.get("name") or contact.get("displayName")
    email = meta.get(config.ZEE_META_EMAIL_KEY) or extract_email(summary or "")
    when = meta.get(config.ZEE_META_WHEN_KEY)
    return {"name": name, "phone": phone, "email": email, "when": when}


# ================= Job de polling (a cada 30 min no GitHub Actions) =================
def _parse_iso(s):
    """Data ISO do ZEE (com 'Z') -> datetime naive em UTC."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "").replace("+00:00", ""))
    except (ValueError, TypeError):
        return None


def _recent_threads(zee, hours_back, now_utc):
    """Conversas relevantes: pagina o GET /threads (que devolve as recentes, 20 por
    página) até cobrir a janela de `hours_back`. Mantém as abertas (endDate nulo) e as
    que finalizaram/começaram na janela."""
    cutoff = now_utc - timedelta(hours=hours_back)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    threads = zee.list_threads(since=cutoff_iso) or []
    out = []
    for t in threads or []:
        end = (t or {}).get("endDate")
        if not end:                       # conversa em andamento
            out.append(t)
            continue
        ref = _parse_iso(end) or _parse_iso((t or {}).get("startDate"))
        if ref is None or ref >= cutoff:
            out.append(t)
    return out


def _unique_contact_ids(threads):
    seen, ids = set(), []
    for t in threads or []:
        cid = (t or {}).get("contactId")
        if cid and cid not in seen:
            seen.add(cid)
            ids.append(cid)
    return ids


def _thread_sort_key(t):
    # ISO 8601 (ex.: "2026-07-15T16:06:58.056Z") ordena cronologicamente como texto.
    return (t or {}).get("startDate") or (t or {}).get("endDate") or ""


def _recent_thread_by_contact(threads):
    """contactId -> a conversa MAIS RECENTE daquele contato (por startDate).
    Assim, com várias interações, usamos o resumo da conversa certa (a de hoje)."""
    best = {}
    for t in threads or []:
        cid = (t or {}).get("contactId")
        if not cid:
            continue
        cur = best.get(cid)
        if cur is None or _thread_sort_key(t) > _thread_sort_key(cur):
            best[cid] = t
    return best


def _norm_tag(s):
    import unicodedata
    s = "".join(c for c in unicodedata.normalize("NFD", str(s or ""))
                if unicodedata.category(c) != "Mn")
    return " ".join(s.lower().split())


def _has_tag(tags, name):
    """Compara tag ignorando acento/maiúscula/espaços extras (evita perder por detalhe)."""
    alvo = _norm_tag(name)
    return any(_norm_tag(t) == alvo for t in (tags or []))


def _final_tag_ids(zee, contact, done_tag_id):
    """Conjunto final de IDs de tag: (tags atuais − 'FX 3') + 'FX 4'.
    O GET /contact devolve os NOMES das tags; usamos GET /tags para mapear
    nome -> id. Retorna None se alguma tag atual não puder ser mapeada (aí é
    mais seguro NÃO fazer o override, pra não apagar tag que não conhecemos)."""
    todo_name = (config.ZEE_TAG_TODO or "").strip()
    todo_id = (config.ZEE_TAG_TODO_ID or "").strip()
    catalog = zee.get_tags() or []
    name_to_id = {(t.get("value") or "").strip(): t.get("id")
                  for t in catalog if t.get("id") and t.get("value")}
    final_ids, seen = [], set()
    for name in (contact.get("tags") or []):
        if not isinstance(name, str):
            return None                       # formato inesperado -> fallback seguro
        nm = name.strip()
        tid = name_to_id.get(nm)
        if nm == todo_name or (tid and tid == todo_id):
            continue                          # remove a 'FX 3 - Agendou AE'
        if not tid:
            return None                       # não sei o id -> não arrisco perder a tag
        if tid not in seen:
            final_ids.append(tid)
            seen.add(tid)
    if done_tag_id and done_tag_id not in seen:
        final_ids.append(done_tag_id)         # garante a 'FX 4 - Feito'
        seen.add(done_tag_id)
    return final_ids


def _mark_done(zee, contact, cid, done_tag_id):
    """Marca 'FX 4 - Feito' e REMOVE 'FX 3 - Agendou AE'.
    O ZEE não tem endpoint de remover tag: usamos set-contact-tag com
    overrideTags=True reenviando o conjunto final (por id), preservando as demais
    tags. Se não der pra montar esse conjunto com segurança, cai no fallback de só
    ADICIONAR 'Feito' (nunca apaga tag que não conseguimos mapear)."""
    contact = contact or {}
    contact_id = contact.get("id") or cid
    if not contact_id:
        return
    if config.ZEE_TAG_TODO_ID and done_tag_id:
        try:
            final_ids = _final_tag_ids(zee, contact, done_tag_id)
            if final_ids is not None:
                zee.set_contact_tag(contact_id, final_ids, override=True)
                return
            log.warning("Tags de %s não mapeadas por completo; só adiciono 'Feito' "
                        "(FX 3 não removida).", cid)
        except Exception as e:
            log.warning("Falha ao trocar FX 3 -> FX 4 em %s: %s; tento só adicionar 'Feito'.",
                        cid, e)
    # fallback (comportamento antigo): só adiciona 'Feito'
    if done_tag_id:
        try:
            zee.set_contact_tag(contact_id, done_tag_id)
        except Exception as e:
            log.warning("Não consegui marcar a tag 'feito' em %s: %s", cid, e)


def _confirm_message(name, when):
    first = (name or "").split()[0] if name else ""
    return config.ZEE_CONFIRM_TEMPLATE.format(first=first, name=first, quando=friendly_when_ptbr(when))


def _write_outbox(cid, name, phone, when, message):
    """Grava a confirmação na fila (JSONL) que o bot do Studio (8550-8065) vai enviar."""
    row = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "contactId": cid, "name": name, "phone": br_phone_with_9(phone),
        "when": when, "message": message, "status": "pending",
    }
    with open(config.STUDIO_OUTBOX_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _confirm_aluna(zee, cid, name, phone, when):
    """Confirmação da aula para a aluna. Vai para a FILA (outbox) que o bot do Studio
    envia pelo 8550-8065. Retorna o canal usado ('outbox'/'zee') ou None."""
    channel = config.STUDIO_CONFIRM_CHANNEL
    if channel == "off":
        return None
    try:
        message = _confirm_message(name, when)
        if channel == "zee":
            if not cid:
                return None
            zee.send_message(cid, message)
            log.info("Confirmação enviada pela ZEE (contato %s)", cid)
            return "zee"
        # padrão: outbox (bot do Studio envia pelo 8550-8065)
        _write_outbox(cid, name, phone, when, message)
        log.info("Confirmação enfileirada no outbox para %s", only_digits(phone))
        return "outbox"
    except Exception as e:
        log.warning("Não consegui preparar a confirmação da aluna: %s", e)
        return None


def process_pending(zee=None, evo=None, hours_back=2, todo_tag=None, done_tag=None,
                    done_tag_id=None, studio_phone=None, dry_run=False, now=None):
    """Loop do job automático: acha contatos com a tag 'Agendou AE' nas conversas
    recentes, agenda no EVO e marca a tag 'Feito'. Idempotente (pula quem já tem 'Feito').

    Filtro por NOME da tag (o GET /contact devolve nomes); a marcação usa o ID."""
    from .zee_client import ZeeClient

    zee = zee or ZeeClient()
    evo = evo or EvoClient()
    todo_tag = todo_tag or config.ZEE_TAG_TODO           # nome
    done_tag = done_tag or config.ZEE_TAG_DONE           # nome
    done_tag_id = done_tag_id or config.ZEE_TAG_DONE_ID  # id
    if not todo_tag:
        raise ValueError("ZEE_TAG_TODO não configurado (tag 'Agendou AE').")

    now_utc = now or datetime.utcnow()
    threads = _recent_threads(zee, hours_back, now_utc)
    results = {"processed": [], "full": [], "skipped": [], "errors": []}

    recent_by_contact = _recent_thread_by_contact(threads)
    for cid, thread in recent_by_contact.items():
        try:
            contact = zee.get_contact(contact_id=cid) or {}
        except Exception as e:
            results["errors"].append({"contactId": cid, "error": str(e)})
            continue

        tags = contact.get("tags") or []
        if not _has_tag(tags, todo_tag):
            continue                                   # não fechou aula experimental
        if done_tag and _has_tag(tags, done_tag):
            continue                                   # já processado

        summary = None
        try:
            # resumo da conversa MAIS RECENTE (evita pegar interação antiga)
            summary = zee.get_summary(cid, thread_id=(thread or {}).get("id"))
        except Exception:
            pass
        data = booking_data_from_contact(contact, summary)
        name, phone, email, when = data["name"], data["phone"], data["email"], data["when"]

        if not name or not when:
            results["skipped"].append({"contactId": cid, "motivo": "faltam nome/horário", **data})
            continue

        if dry_run:
            results["processed"].append({"contactId": cid, "dryRun": True, **data})
            continue

        try:
            res = book_experimental(name=name, when=when, email=email, phone=phone, evo=evo)
            _mark_done(zee, contact, cid, done_tag_id)
            canal = _confirm_aluna(zee, cid, name, phone, res.when)
            results["processed"].append({
                "contactId": cid, "idProspect": res.id_prospect, "when": res.when,
                "idConfiguration": res.id_configuration, "confirmacao": canal,
            })
        except TurmaLotadaError as e:
            notify_studio_full(zee, name=name, phone=phone, when=e.when, reason=e.reason,
                               alternatives=e.alternatives, studio_phone=studio_phone)
            _mark_done(zee, contact, cid, done_tag_id)  # evita re-avisar o Studio a cada ciclo
            results["full"].append({"contactId": cid, "when": e.when,
                                    "motivo": e.reason, "alternatives": e.alternatives})
        except Exception as e:
            results["errors"].append({"contactId": cid, "name": name, "error": str(e)})

    log.info("Processados=%d, lotados=%d, pulados=%d, erros=%d",
             len(results["processed"]), len(results["full"]),
             len(results["skipped"]), len(results["errors"]))
    return results
