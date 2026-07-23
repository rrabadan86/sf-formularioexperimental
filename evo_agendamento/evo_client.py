# Cliente da API do EVO (W12 / abcevo).
# Doc: https://evo-integracao.w12app.com.br/swagger  |  Auth: Basic (DNS + Secret Key).
import logging

import requests
from requests.auth import HTTPBasicAuth

from . import config
from .util import br_phone_with_9, build_session, fmt_date_evo, fmt_datetime_evo, only_digits

log = logging.getLogger("evo")


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
        resp = self.session.request(
            method, url,
            params=_drop_empty(params), json=json, auth=self.auth,
            timeout=self.timeout, headers={"Accept": "application/json"},
        )
        if not resp.ok:
            raise EvoError(f"EVO {method} {path} -> HTTP {resp.status_code}: {_error_detail(resp)}")
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def _bid(self, branch_id):
        return branch_id if branch_id is not None else self.branch_id

    # --------------- prospects (cadastro) ---------------
    def find_prospects(self, email=None, phone=None, normalize_phone=True):
        """Busca prospects por e-mail e/ou telefone. Retorna lista (pode ser vazia).
        normalize_phone=False usa o telefone exatamente como veio (sem re-inserir o
        9), permitindo buscar variações (com e sem o 9) de cadastros antigos."""
        params = {"take": 50}
        if email:
            params["email"] = email
        if phone:
            params["phone"] = _evo_cellphone(phone, config.EVO_DDI) if normalize_phone else only_digits(phone)
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
            "document": only_digits(document) if document else None,
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

    def _search_prospect_id(self, email=None, phone=None, log_hit=False):
        """Procura um prospect existente: primeiro por e-mail, depois por telefone —
        testando o celular COM e SEM o 9. Cadastros antigos podem ter sido salvos sem
        o 9 do celular (ex.: 6293185183 em vez de 62993185183); procurar as duas
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
        return None

    def get_or_create_prospect(self, name, last_name=None, email=None, phone=None,
                               ddi=None, branch_id=None, document=None, birthday=None):
        """Idempotência: reaproveita prospect existente (por e-mail, depois telefone
        com/sem o 9) ou cria um novo. Retorna (idProspect, criado?)."""
        idp = self._search_prospect_id(email=email, phone=phone, log_hit=True)
        if idp:
            return idp, False
        created = self.create_prospect(name, last_name, email, phone, ddi, branch_id,
                                       document=document, birthday=birthday)
        return created, True

    def find_prospect_id(self, email=None, phone=None):
        """Retorna o idProspect de um prospect existente (por e-mail, depois telefone
        com/sem o 9), ou None se não achar. NÃO cria."""
        return self._search_prospect_id(email=email, phone=phone)

    def prospect_services(self, id_prospect, branch_id=None):
        """Serviços já vendidos para um prospect: lista de {idService, nameService}."""
        params = {"idProspect": int(id_prospect)}
        bid = self._bid(branch_id)
        if bid:
            params["idBranch"] = bid
        data = self._request("GET", "/api/v1/prospects/services", params=params)
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
