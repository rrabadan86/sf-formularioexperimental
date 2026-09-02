# Cliente da API do EVO (W12 / abcevo).
# Doc: https://evo-integracao.w12app.com.br/swagger  |  Auth: Basic (DNS + Secret Key).
import logging
import threading
import time
from collections import deque

import requests
from requests.auth import HTTPBasicAuth

from . import config
from .util import br_phone_with_9, build_session, fmt_date_evo, fmt_datetime_evo, only_digits

log = logging.getLogger("evo")

# --- Limitador de taxa -------------------------------------------------------
# O EVO devolve HTTP 429 acima de 40 requisições por minuto. Mantemos uma folga
# (35/min) e SERIALIZAMOS os envios deste processo por uma janela deslizante de
# 60s, pra nunca estourar a cota por conta própria (a grade do formulário fazia
# uma chamada /detail por horário da semana e estourava o limite).
_RATE_MAX = 35
_RATE_WINDOW = 60.0
_rate_lock = threading.Lock()
_rate_hits = deque()


def _rate_gate():
    """Bloqueia até haver "vaga" na janela de 60s (no máximo _RATE_MAX chamadas)."""
    with _rate_lock:
        now = time.monotonic()
        while _rate_hits and now - _rate_hits[0] > _RATE_WINDOW:
            _rate_hits.popleft()
        if len(_rate_hits) >= _RATE_MAX:
            espera = _RATE_WINDOW - (now - _rate_hits[0]) + 0.05
            if espera > 0:
                log.info("Throttle EVO: aguardando %.1fs para respeitar o limite de req/min...", espera)
                time.sleep(espera)
            now = time.monotonic()
            while _rate_hits and now - _rate_hits[0] > _RATE_WINDOW:
                _rate_hits.popleft()
        _rate_hits.append(time.monotonic())


class EvoError(RuntimeError):
    """Erro retornado pela API do EVO (com as mensagens quando disponíveis)."""


class EvoClient:
    def __init__(self, dns=None, token=None, base_url=None, branch_id=None, timeout=None):
        self.base_url = (base_url or config.EVO_BASE_URL).rstrip("/")
        dns = dns if dns is not None else config.EVO_DNS
        token = token if token is not None else config.EVO_TOKEN
        if not dns or not token:
            raise EvoError("EVO_DNS e EVO_TOKEN são obrigatórios (Basic auth).")
        self.auth = HTTPBasicAuth(dns, token)
        _branch = branch_id if branch_id is not None else config.EVO_BRANCH_ID
        self.branch_id = int(_branch) if str(_branch).strip() else None
        self.timeout = timeout or config.EVO_TIMEOUT
        self.session = build_session()

    # --------------- baixo nível ---------------
    def _request(self, method, path, params=None, json=None):
        url = f"{self.base_url}{path}"
        for tentativa in range(1, 5):                 # 1 tentativa + 3 retries no 429
            _rate_gate()                              # respeita o limite de req/min
            resp = self.session.request(
                method, url,
                params=_drop_empty(params), json=json, auth=self.auth,
                timeout=self.timeout, headers={"Accept": "application/json"},
            )
            # HTTP 429 = limite de 40/min do EVO. Espera e tenta de novo (pode ter
            # sido outro consumidor — ex.: o bot do PC — usando a cota ao mesmo tempo).
            if resp.status_code == 429 and tentativa < 4:
                ra = resp.headers.get("Retry-After")
                try:
                    espera = float(ra) if ra else min(8 * tentativa, 30)
                except (TypeError, ValueError):
                    espera = min(8 * tentativa, 30)
                # Piso de espera: o EVO às vezes devolve Retry-After: 0, e repetir
                # na hora só toma 429 de novo (foi o que esvaziou as 3 tentativas
                # instantaneamente). Espera crescente para a cota se recuperar.
                espera = max(espera, min(5 * tentativa, 30))
                log.warning("EVO 429 (limite 40/min). Aguardando %.0fs e tentando de novo (%d/3)...",
                            espera, tentativa)
                time.sleep(espera)
                continue
            # HTTP 5xx = erro TEMPORÁRIO do servidor do EVO (W12), não da nossa
            # requisição (auth/params dariam 401/400/422). Em vez de derrubar o job
            # inteiro (ex.: o push_slots da grade, que roda a cada 10 min), espera
            # um pouco e tenta de novo — como no 429. Se persistir, aí sim levanta.
            if resp.status_code >= 500 and tentativa < 4:
                espera = min(5 * tentativa, 20)
                log.warning("EVO %d (erro temporário do servidor). Aguardando %.0fs e tentando de novo (%d/3)...",
                            resp.status_code, espera, tentativa)
                time.sleep(espera)
                continue
            if not resp.ok:
                raise EvoError(f"EVO {method} {path} -> HTTP {resp.status_code}: {_error_detail(resp)}")
            if resp.status_code == 204 or not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return resp.text
        # Esgotou os retries ainda em 429:
        raise EvoError(f"EVO {method} {path} -> HTTP 429: limite de 40 req/min excedido após 3 tentativas")

    def _bid(self, branch_id):
        return branch_id if branch_id is not None else self.branch_id

    # --------------- prospects (cadastro) ---------------
    def find_prospects(self, email=None, phone=None, document=None, normalize_phone=True):
        """Busca prospects por e-mail, telefone e/ou CPF. Retorna lista (pode ser vazia).
        normalize_phone=False usa o telefone exatamente como veio (sem re-inserir o
        9), permitindo buscar variações (com e sem o 9) de cadastros antigos.
        document = CPF (só dígitos) — o EVO filtra prospects pelo campo "document"."""
        params = {"take": 50}
        if email:
            params["email"] = email
        if phone:
            params["phone"] = _evo_cellphone(phone, config.EVO_DDI) if normalize_phone else only_digits(phone)
        if document:
            params["document"] = only_digits(document)
        data = self._request("GET", "/api/v1/prospects", params=params) or []
        return data if isinstance(data, list) else []

    def create_prospect(self, name, last_name=None, email=None, phone=None,
                        ddi=None, branch_id=None, notes=None, document=None, birthday=None):
        """Cadastra um prospect (aluno em potencial). Retorna idProspect.
        document = CPF (só dígitos); birthday = data de nascimento (yyyy-MM-dd)."""
        body = {
            "name": name,
            "lastName": last_name,
            "email": email,
            "cellphone": _evo_cellphone(phone, ddi or config.EVO_DDI),
            "ddi": ddi or config.EVO_DDI,
            # ATENÇÃO: o EVO usa nomes diferentes na entrada e na saída. O CPF é
            # enviado como "cpf" e devolvido como "document" (assim como
            # "birthday" volta como "birthDate"). Enviar "document" aqui faz o
            # EVO ignorar o campo em silêncio — o cadastro ficava sem CPF.
            "cpf": only_digits(document) if document else None,
            "birthday": birthday or None,
            "notes": notes,
        }
        bid = self._bid(branch_id)
        if bid:
            body["idBranch"] = bid
        data = self._request("POST", "/api/v1/prospects", json=_drop_empty(body))
        id_prospect = (data or {}).get("idProspect")
        if not id_prospect:
            raise EvoError(f"Cadastro não retornou idProspect: {data!r}")
        log.info("Prospect criado: idProspect=%s (%s)", id_prospect, name)
        return id_prospect

    def update_prospect(self, id_prospect, name=None, last_name=None, email=None,
                        phone=None, ddi=None, document=None, birthday=None,
                        branch_id=None):
        """Atualiza os dados de um prospect JÁ existente (nome, e-mail, celular,
        CPF, nascimento). Serve para completar cadastros antigos que vieram sem
        essas informações quando a pessoa reagenda pelo formulário.

        BEST-EFFORT: nunca levanta exceção — se a API recusar (endpoint/verbo
        diferente na sua conta EVO), devolve um dicionário com o motivo, e o
        agendamento segue normalmente. Retorna:
          {"ok": True,  "verbo": "PATCH"|"PUT", "campos": [...]}   em caso de sucesso
          {"ok": False, "erro": "..."}                              se não deu
        Só envia os campos que vieram preenchidos (não apaga o que já existe)."""
        body = {
            "idProspect": int(id_prospect),
            "name": name or None,
            "lastName": last_name or None,
            "email": email or None,
            "cellphone": _evo_cellphone(phone, ddi or config.EVO_DDI) if phone else None,
            "ddi": (ddi or config.EVO_DDI) if phone else None,
            "cpf": only_digits(document) if document else None,
            "birthday": birthday or None,
        }
        bid = self._bid(branch_id)
        if bid:
            body["idBranch"] = bid
        body = _drop_empty(body)
        campos = [k for k in ("name", "lastName", "email", "cellphone", "cpf", "birthday") if k in body]
        if not campos:
            return {"ok": False, "erro": "sem campos para atualizar"}
        # O EVO/W12 expõe a atualização de prospect de formas diferentes conforme
        # a conta. Tentamos os caminhos mais comuns, em ordem, e paramos no 1º que
        # der certo. Qualquer falha é engolida (best-effort) e reportada de volta.
        tentativas = [
            ("PATCH", f"/api/v1/prospects/{int(id_prospect)}"),
            ("PUT",   f"/api/v1/prospects/{int(id_prospect)}"),
            ("PATCH", "/api/v1/prospects"),
        ]
        ultimo_erro = None
        for verbo, path in tentativas:
            try:
                self._request(verbo, path, json=body)
                log.info("Prospect %s atualizado (%s %s): %s", id_prospect, verbo, path, campos)
                return {"ok": True, "verbo": verbo, "path": path, "campos": campos}
            except EvoError as e:
                ultimo_erro = str(e)
                # 404/405 = esse caminho não existe nessa conta → tenta o próximo.
                # 400/422 = caminho existe mas o corpo não bateu → não adianta
                # insistir nos outros; para aqui com o motivo real.
                if any(c in ultimo_erro for c in ("HTTP 400", "HTTP 422")):
                    break
        log.warning("Não consegui atualizar o prospect %s: %s", id_prospect, ultimo_erro)
        return {"ok": False, "erro": ultimo_erro or "falha desconhecida"}

    def _search_prospect_id(self, email=None, phone=None, document=None, log_hit=False):
        """Procura um prospect existente: por e-mail, depois telefone (testando o
        celular COM e SEM o 9), depois CPF. Cadastros antigos podem ter sido salvos
        sem o 9 do celular (ex.: 6293185183 em vez de 62993185183); procurar as duas
        formas evita criar um duplicado. Retorna idProspect ou None."""
        if email:
            found = self.find_prospects(email=email)
            if found and found[0].get("idProspect"):
                idp = found[0]["idProspect"]
                if log_hit:
                    log.info("Prospect já existe (email=%s): idProspect=%s", email, idp)
                return idp
        if phone:
            for tel in _evo_cellphone_variants(phone, config.EVO_DDI):
                found = self.find_prospects(phone=tel, normalize_phone=False)
                if found and found[0].get("idProspect"):
                    idp = found[0]["idProspect"]
                    if log_hit:
                        log.info("Prospect já existe (phone=%s): idProspect=%s", tel, idp)
                    return idp
        if document:
            doc = only_digits(document)
            if doc:
                # Confirma que o CPF do prospect retornado realmente bate — se o EVO
                # ignorar o filtro "document" e devolver uma lista genérica, isso evita
                # bloquear a pessoa errada por engano.
                for p in self.find_prospects(document=doc):
                    if only_digits(p.get("document") or "") == doc and p.get("idProspect"):
                        idp = p["idProspect"]
                        if log_hit:
                            log.info("Prospect já existe (cpf): idProspect=%s", idp)
                        return idp
        return None

    def get_or_create_prospect(self, name, last_name=None, email=None, phone=None,
                               ddi=None, branch_id=None, document=None, birthday=None):
        """Idempotência: reaproveita prospect existente (por e-mail, depois telefone
        com/sem o 9, depois CPF) ou cria um novo. Retorna (idProspect, criado?)."""
        idp = self._search_prospect_id(email=email, phone=phone, document=document, log_hit=True)
        if idp:
            return idp, False
        created = self.create_prospect(name, last_name, email, phone, ddi, branch_id,
                                       document=document, birthday=birthday)
        return created, True

    def find_prospect_id(self, email=None, phone=None, document=None):
        """Retorna o idProspect de um prospect existente (por e-mail, depois telefone
        com/sem o 9, depois CPF), ou None se não achar. NÃO cria."""
        return self._search_prospect_id(email=email, phone=phone, document=document)

    def prospect_services(self, id_prospect, branch_id=None):
        """Serviços já vendidos para um prospect: lista de {idService, nameService}."""
        params = {"idProspect": int(id_prospect)}
        bid = self._bid(branch_id)
        if bid:
            params["idBranch"] = bid
        data = self._request("GET", "/api/v1/prospects/services", params=params)
        return data if isinstance(data, list) else []

    # --------------- members (alunas contratadas) — leitura ---------------
    def find_members(self, phone=None, document=None, name=None, normalize_phone=True,
                     show_memberships=True, branch_id=None):
        """Busca ALUNAS (members) por telefone, CPF ou nome. Retorna lista (pode ser
        vazia). É o cadastro de aluna CONTRATADA — diferente de prospect/lead.
        Endpoint: GET /api/v2/members. status=1 = ativas (inclui suspensas/VIPs)."""
        params = {"take": 10, "status": 1}
        if phone:
            params["phone"] = _evo_cellphone(phone, config.EVO_DDI) if normalize_phone else only_digits(phone)
        if document:
            params["document"] = only_digits(document)
        if name:
            params["name"] = name
        if show_memberships:
            params["showMemberships"] = "true"
            params["showActivityData"] = "true"
        bid = self._bid(branch_id)
        if bid:
            params["idBranch"] = bid
        data = self._request("GET", "/api/v2/members", params=params) or []
        return data if isinstance(data, list) else ([data] if data else [])

    def find_member_id(self, phone=None, document=None):
        """Atalho: idMember da 1ª aluna que casar (telefone com/sem o 9, depois CPF),
        ou None. NÃO cria nada — só consulta."""
        for np in (True, False):
            membros = self.find_members(phone=phone, document=document, normalize_phone=np)
            if membros:
                m = membros[0]
                return m.get("idMember") or m.get("idMembro")
            if not phone:
                break
        return None

    def member_contract(self, id_member, branch_id=None):
        """Resumo do CONTRATO VIGENTE da aluna, pronto para a Sofia responder.

        Regras do Studio:
          - "vigente" NAO e so status "active": um contrato TRANCADO fica como
            "suspended" e continua sendo o contrato dela;
          - planos de CIRCUITO SLIM sao ignorados (assunto de recepcao) — sem isso
            o Circuito "active" mascararia o plano principal suspenso;
          - o limite de trancamento e de 30 dias por contrato (daysLeftToFreeze
            traz esse total permitido; o que resta e o total menos o ja usado).

        Retorna dict com plano, vigencia, situacao, trancamento, reposicoes e
        proxima cobranca. Sem contrato vigente, devolve {"tem_contrato": False}."""
        from datetime import datetime as _dt
        def _d(v):
            try: return _dt.strptime(str(v)[:10], "%Y-%m-%d").date()
            except Exception: return None
        hoje = _dt.now().date()

        dados = self._request("GET", "/api/v2/members", params={
            "idsMembers": str(int(id_member)), "showMemberships": "true",
            # sem showActivityData o EVO omite o weeklyLimit (o "3x por semana")
            "showActivityData": "true", "take": 1,
        }) or []
        m = (dados[0] if isinstance(dados, list) and dados else dados) or {}
        contratos = m.get("memberships") or []

        # fora os Circuitos (recepcao cuida) e fora os encerrados/cancelados
        VIGENTES = ("active", "suspended")
        elegiveis = [c for c in contratos
                     if "circuito" not in str(c.get("name") or "").lower()
                     and str(c.get("membershipStatus") or "").lower() in VIGENTES]
        if not elegiveis:
            return {"tem_contrato": False,
                    "nome": (m.get("firstName") or "").strip(),
                    "idMember": int(id_member)}
        # o de termino mais distante e o contrato em vigor
        c = sorted(elegiveis, key=lambda x: (_d(x.get("endDate")) or hoje))[-1]

        freezes = c.get("freezes") or []
        usados = sum(int(f.get("daysFreeze") or 0) for f in freezes)
        permitido = int(c.get("daysLeftToFreeze") or 0)          # total do contrato (30 nos planos FREE)
        atual = next((f for f in freezes
                      if (_d(f.get("startSuspend")) or hoje) <= hoje <= (_d(f.get("endSuspend")) or hoje)), None)
        fim = _d(c.get("endDate"))
        return {
            "tem_contrato": True,
            "idMember": int(id_member),
            "nome": (m.get("firstName") or "").strip(),
            "plano": str(c.get("name") or "").strip(),
            "vezes_por_semana": c.get("weeklyLimit"),
            "inicio": str(c.get("startDate") or "")[:10],
            "termino": str(c.get("endDate") or "")[:10],
            "dias_para_terminar": ((fim - hoje).days if fim else None),
            "situacao": str(c.get("membershipStatus") or "").lower(),
            "trancado_agora": bool(atual),
            "trancado_ate": (str(atual.get("endSuspend"))[:10] if atual else None),
            "trancamento_dias_usados": usados,
            "trancamento_dias_permitidos": permitido,
            "trancamento_dias_restantes": max(0, permitido - usados),
            "reposicoes_disponiveis": int(c.get("pendingRepositions") or 0),
            "proxima_cobranca": (str(c.get("nextCharge"))[:10] if c.get("nextCharge") else None),
            "valor_proxima_cobranca": c.get("valueNextMonth"),
        }

    def member_sessions(self, id_member, date_start=None, date_end=None, days_ahead=60,
                        branch_id=None, take=50):
        """Agenda da ALUNA: sessões FUTURAS que ela marcou (SLIMFIT etc.). O endpoint
        EXIGE um intervalo de datas — sem ele volta vazio. Padrão: de hoje até
        +days_ahead. Endpoint: GET /api/v2/activities/member/sessions. Só leitura.
        Campos úteis: activitieName, date, startTime/endTime, idConfiguration,
        idActivitieSession, statusName, isReplacement, faltaJustificada."""
        from datetime import datetime as _dt, timedelta as _td
        d0 = date_start or _dt.now().strftime("%Y-%m-%d")
        d1 = date_end or (_dt.now() + _td(days=days_ahead)).strftime("%Y-%m-%d")
        params = {"idMember": int(id_member), "dateStart": d0, "dateEnd": d1, "take": take}
        bid = self._bid(branch_id)
        if bid:
            params["idBranch"] = bid
        data = self._request("GET", "/api/v2/activities/member/sessions", params=params) or []
        return data if isinstance(data, list) else []

    # --------------- serviços / horários (descoberta) ---------------
    def list_services(self, experimental_only=False, branch_id=None):
        """Lista serviços. Com experimental_only=True, filtra os que liberam aula
        experimental (flag experimentalClass)."""
        params = {"take": 50, "showActivities": True}
        bid = self._bid(branch_id)
        if bid:
            params["idBranch"] = bid
        data = self._request("GET", "/api/v1/service", params=params) or []
        if experimental_only:
            data = [s for s in data if s.get("experimentalClass")]
        return data

    def list_activities(self, search="", branch_id=None):
        """Lista atividades (aulas) da academia."""
        params = {"take": 50, "search": search}
        bid = self._bid(branch_id)
        if bid:
            params["idBranch"] = bid
        return self._request("GET", "/api/v1/activities", params=params) or []

    def list_experimental_schedule(self, date, show_full_week=True, branch_id=None):
        """Horários que aceitam aula experimental a partir de uma data.
        Retorna itens com allowExperimentalClass / experimentalClassSlots / activityDate."""
        params = {
            "experimentalClass": True,
            "date": fmt_date_evo(date),
            "showFullWeek": show_full_week,
            "onlyAvailables": True,
        }
        bid = self._bid(branch_id)
        if bid:
            params["idBranch"] = bid
        return self._request("GET", "/api/v1/activities/schedule", params=params) or []

    def list_schedule(self, date, show_full_week=False, only_availables=False, branch_id=None):
        """Agenda de turmas (sem filtro de aula experimental). Traz idConfiguration,
        startTime, capacity e ocupation — usada para achar a turma do horário e checar vaga."""
        params = {
            "date": fmt_date_evo(date),
            "showFullWeek": show_full_week,
            "onlyAvailables": only_availables,
        }
        bid = self._bid(branch_id)
        if bid:
            params["idBranch"] = bid
        return self._request("GET", "/api/v1/activities/schedule", params=params) or []

    def schedule_detail(self, id_configuration=None, activity_date=None, id_session=None,
                        branch_id=None):
        """Detalhe de UMA turma (sessão de um dia), com a lista `enrollments` — cada
        participante traz idMember, idProspect, status, etc. É assim que sabemos quantas
        aulas EXPERIMENTAIS (idProspect preenchido) já existem naquele horário.

        Endpoint: GET /api/v1/activities/schedule/detail
        Passe (id_configuration + activity_date) OU id_session (idAtividadeSessao).
        Obs.: NÃO envie idConfiguration e idActivitySession juntos (a API devolve 204)."""
        if id_session is not None:
            params = {"idActivitySession": int(id_session)}
        else:
            params = {"idConfiguration": int(id_configuration),
                      "activityDate": fmt_date_evo(activity_date)}
        bid = self._bid(branch_id)
        if bid:
            params["idBranch"] = bid
        return self._request("GET", "/api/v1/activities/schedule/detail", params=params) or {}

    # --------------- venda do serviço ---------------
    def create_sale(self, id_service, id_prospect=None, id_member=None,
                    service_value=0.0, payment=None, total_installments=1, branch_id=None):
        """Vende um serviço (ex.: 'Aula Experimental') para um prospect/aluno.
        Espelha o passo manual de 'vender o serviço para a oportunidade'."""
        body = {
            "idService": int(id_service),
            "serviceValue": service_value,
            "totalInstallments": total_installments,
        }
        if id_prospect:
            body["idProspect"] = int(id_prospect)
        if id_member:
            body["idMember"] = int(id_member)
        if payment not in (None, ""):
            body["payment"] = int(payment)
        bid = self._bid(branch_id)
        if bid:
            body["idBranch"] = bid
        data = self._request("POST", "/api/v1/sales", json=body)
        log.info("Serviço vendido (idService=%s) para prospect=%s", id_service, id_prospect)
        return data

    # --------------- matrícula na turma (agendamento normal) ---------------
    def enroll_schedule(self, id_configuration, activity_date, id_prospect=None,
                        id_member=None, slot_number=0, origin=None):
        """Matricula o prospect/aluno numa turma (agendamento normal).
        activity_date: data da turma (yyyy-MM-dd); a turma é identificada pelo idConfiguration."""
        params = {
            "idConfiguration": int(id_configuration),
            "activityDate": fmt_date_evo(activity_date),
            "slotNumber": slot_number,
        }
        if id_prospect:
            params["idProspect"] = int(id_prospect)
        if id_member:
            params["idMember"] = int(id_member)
        if origin is not None:
            params["origin"] = origin
        data = self._request("POST", "/api/v1/activities/schedule/enroll", params=params)
        log.info("Matriculado na turma idConfiguration=%s em %s (prospect=%s)",
                 id_configuration, params["activityDate"], id_prospect)
        return data

    def change_session_status(self, status, id_member, id_configuration=None,
                              activity_date=None, id_activity_session=None, branch_id=None):
        """Muda o status da aluna numa sessão: 0=Presente, 1=Falta, 2=Falta JUSTIFICADA.
        A falta justificada é o que gera reposição (conforme as regras do EVO).
        Endpoint: POST /api/v1/activities/schedule/enroll/change-status."""
        params = {"status": int(status), "idMember": int(id_member)}
        if id_activity_session:
            params["idActivitySession"] = int(id_activity_session)
        else:
            params["idConfiguration"] = int(id_configuration)
            params["activityDate"] = fmt_date_evo(activity_date)
        bid = self._bid(branch_id)
        if bid:
            params["idBranch"] = bid
        return self._request("POST", "/api/v1/activities/schedule/enroll/change-status", params=params)

    def unenroll_member(self, id_member, id_configuration_participation=None,
                        id_employee=None, branch_id=None):
        """APAGA A MATRICULA da aluna numa turma (DELETE /api/v1/activities/enrollment).

        ATENCAO: isto remove a MATRICULA RECORRENTE — a aluna sai de TODAS as
        ocorrencias futuras daquela turma, nao de um dia so. Para desmarcar
        apenas UMA aula, use cancelar_sessao() (change-status), que e reversivel.

        Parametros exigidos pelo EVO (todos na query): idMember, idEmployee e
        idConfigurationParticipation (o id da PARTICIPACAO, nao o da turma).
        BEST-EFFORT: nao levanta excecao."""
        emp = id_employee or config.EVO_ID_EMPLOYEE
        if not emp:
            return {"ok": False, "erro": "EVO_ID_EMPLOYEE nao configurado"}
        if not id_configuration_participation:
            return {"ok": False, "erro": "idConfigurationParticipation e obrigatorio"}
        bid = self._bid(branch_id)
        base = {"idMember": int(id_member), "idEmployee": int(emp)}
        if bid:
            base["idBranch"] = bid
        # v1 usa idConfigurationParticipation; v2 usa idConfigurationEnroll — mesmo
        # numero, nomes diferentes. Tenta v1 e cai para v2.
        alvo = str(id_configuration_participation)
        erros = []
        for caminho, campo in (("/api/v1/activities/enrollment", "idConfigurationParticipation"),
                               ("/api/v2/activities/enroll", "idConfigurationEnroll")):
            try:
                self._request("DELETE", caminho, params=dict(base, **{campo: alvo}))
                log.info("Matricula %s da aluna %s apagada via %s", alvo, id_member, caminho)
                return {"ok": True, "via": f"DELETE {caminho} ({campo})"}
            except Exception as e:
                erros.append(f"{caminho}: {str(e)[:140]}")
        return {"ok": False, "erro": "nao foi possivel apagar a matricula", "tentativas": erros}

    def cancelar_sessao(self, id_member, status=2, id_configuration=None,
                        activity_date=None, id_activity_session=None, branch_id=None):
        """Desmarca a aluna de UMA aula mudando o status da participacao.
        status: 0=Presente, 1=Falta, 2=Falta JUSTIFICADA (gera reposicao conforme
        as regras do EVO). E REVERSIVEL: basta chamar de novo com status=0.
        BEST-EFFORT: nao levanta excecao."""
        try:
            self.change_session_status(status=status, id_member=id_member,
                                       id_configuration=id_configuration,
                                       activity_date=activity_date,
                                       id_activity_session=id_activity_session,
                                       branch_id=branch_id)
            return {"ok": True, "via": f"change-status status={status}"}
        except Exception as e:
            return {"ok": False, "erro": str(e)[:300]}

    # --------------- agendamento da aula experimental ---------------
    def book_experimental_class(self, id_prospect, activity_date, activity=None,
                                service=None, id_activity=None, id_service=None,
                                branch_id=None, activity_exist=None):
        """Cria a aula experimental, vende o serviço e matricula o prospect.
        Um único endpoint cobre venda + agendamento. Retorna idActivitySession.

        Identifique a atividade/serviço por nome (activity/service) OU por id
        (id_activity/id_service). activity_date: 'yyyy-MM-dd HH:mm' (ou datetime)."""
        params = {
            "idProspect": int(id_prospect),
            "activityDate": fmt_datetime_evo(activity_date),
            "activity": activity,
            "service": service,
        }
        if id_activity:
            params["idActivity"] = int(id_activity)
        if id_service:
            params["idService"] = int(id_service)
        if activity_exist is not None:
            params["activityExist"] = bool(activity_exist)
        bid = self._bid(branch_id)
        if bid:
            params["idBranch"] = bid
        data = self._request("POST", "/api/v1/activities/schedule/experimental-class", params=params)
        id_session = (data or {}).get("idActivitySession")
        log.info("Aula experimental agendada: idActivitySession=%s (prospect=%s, %s)",
                 id_session, id_prospect, params["activityDate"])
        return id_session


# ---------------- helpers de módulo ----------------
def _drop_empty(d):
    if not d:
        return d
    return {k: v for k, v in d.items() if v not in (None, "")}


def _evo_cellphone(phone, ddi):
    """Número para o EVO SEM o DDI no começo. O ZEE entrega o telefone já com o
    código do país (ex.: '556282809212'); como o EVO tem um campo 'ddi' separado,
    mandar o número inteiro duplicava o 55 (+55 55 62 ...). Só remove o DDI se
    sobrar um número brasileiro válido (>=10 dígitos: DDD + número)."""
    digits = br_phone_with_9(phone)          # garante o 9 do celular (ZEE às vezes grava sem)
    ddi = only_digits(str(ddi or ""))
    if ddi and digits.startswith(ddi) and len(digits) - len(ddi) >= 10:
        return digits[len(ddi):]
    return digits


def _evo_cellphone_variants(phone, ddi):
    """Formas do celular (SEM DDI) para BUSCAR duplicados: com o 9 e sem o 9.
    Cadastros antigos podem ter sido salvos sem o 9 do celular (ex.: 6293185183);
    procurar as duas formas evita criar um prospect duplicado."""
    base = _evo_cellphone(phone, ddi)            # local, com o 9 garantido
    variants = [base]
    # celular com 9 = DDD (2) + 9 + 8 dígitos = 11 dígitos começando o 3º com "9"
    if len(base) == 11 and base[2] == "9":
        sem9 = base[:2] + base[3:]               # remove o 9 -> forma antiga (sem o 9)
        if sem9 not in variants:
            variants.append(sem9)
    return variants


def _error_detail(resp):
    try:
        data = resp.json()
        if isinstance(data, dict):
            if data.get("mensagens"):
                return "; ".join(str(m) for m in data["mensagens"])
            # EVO costuma devolver {"errors": [{"value": "..."}]}
            if isinstance(data.get("errors"), list) and data["errors"]:
                msgs = [str(e.get("value") or e.get("message") or e)
                        for e in data["errors"] if isinstance(e, dict)]
                if msgs:
                    return "; ".join(msgs)
            if data.get("message"):
                return str(data["message"])
            if data.get("detail"):
                return str(data["detail"])
    except ValueError:
        pass
    return (resp.text or "").strip()[:500]
