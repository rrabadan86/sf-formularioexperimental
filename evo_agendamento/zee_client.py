# Cliente da API do ZEE (IA de primeiro atendimento / WhatsApp CRM).
# Só é usado no modo automático (puxar dados do ZEE). O agendamento em si é no EVO.
#
# ATENÇÃO: o esquema de autenticação exato do ZEE ainda precisa ser confirmado
# (botão "Authorize" da doc). Por isso o header/scheme são configuráveis via env
# (ZEE_AUTH_HEADER / ZEE_AUTH_SCHEME / ZEE_TOKEN).
import logging

import requests

from . import config
from .util import build_session

log = logging.getLogger("zee")


class ZeeError(RuntimeError):
    pass


class ZeeClient:
    def __init__(self, token=None, base_url=None, auth_header=None, auth_scheme=None, timeout=None):
        self.base_url = (base_url or config.ZEE_BASE_URL).rstrip("/")
        token = token if token is not None else config.ZEE_TOKEN
        if not token:
            raise ZeeError("ZEE_TOKEN é obrigatório para o modo automático.")
        header = auth_header or config.ZEE_AUTH_HEADER
        scheme = auth_scheme if auth_scheme is not None else config.ZEE_AUTH_SCHEME
        value = f"{scheme} {token}".strip() if scheme else token
        self.headers = {header: value, "Accept": "application/json"}
        self.timeout = timeout or config.ZEE_TIMEOUT
        self.session = build_session()

    def _request(self, method, path, params=None, json=None):
        url = f"{self.base_url}{path}"
        resp = self.session.request(method, url, params=params, json=json,
                                    headers=self.headers, timeout=self.timeout)
        if not resp.ok:
            raise ZeeError(f"ZEE {method} {path} -> HTTP {resp.status_code}: {(resp.text or '')[:500]}")
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # GET /contact?phone=&contactId=
    def get_contact(self, phone=None, contact_id=None):
        params = {}
        if phone:
            params["phone"] = phone
        if contact_id:
            params["contactId"] = contact_id
        return self._request("GET", "/contact", params=params)

    # GET /threads?contactId=&status=&startDate=&endDate=&page=&pageSize=
    def list_threads(self, contact_id=None, status=None, start_date=None, end_date=None,
                     page=None, page_size=None):
        params = {
            "contactId": contact_id, "status": status,
            "startDate": start_date, "endDate": end_date,
            "page": page, "pageSize": page_size,
        }
        params = {k: v for k, v in params.items() if v not in (None, "")}
        return self._request("GET", "/threads", params=params) or []

    # GET /summary/{contactId}?threadId=
    def get_summary(self, contact_id, thread_id=None):
        params = {"threadId": thread_id} if thread_id else None
        data = self._request("GET", f"/summary/{contact_id}", params=params) or {}
        return data.get("summary") if isinstance(data, dict) else None

    # GET /messages/{threadId}
    def get_messages(self, thread_id, page=None, page_size=None):
        params = {k: v for k, v in {"page": page, "pageSize": page_size}.items() if v}
        return self._request("GET", f"/messages/{thread_id}", params=params) or {}

    # GET /tags  -> catálogo de tags [{ id, value, ... }] (para mapear nome -> id)
    def get_tags(self):
        return self._request("GET", "/tags") or []

    # PUT /set-contact-tag  { contactId, tags: [] }
    def set_contact_tag(self, contact_id, tags, override=False):
        if isinstance(tags, str):
            tags = [tags]
        return self._request(
            "PUT", "/set-contact-tag",
            params={"overrideTags": override},
            json={"contactId": contact_id, "tags": tags},
        )

    # POST /contact  -> cria contato (retorna { id, ... })
    #def create_contact(self, phone, name=None):
        #from .util import only_digits
        #body = {"phone": only_digits(phone), "provider": "z-api"}
        #if name:
            #body["name"] = name
        #return self._request("POST", "/contact", json=body)

    def create_contact(self, phone, name=None):
        from .util import only_digits
        digits = only_digits(phone)
        body = {"phone": digits, "provider": "z-api", "displayName": name or digits}
        return self._request("POST", "/contact", json=body)

    def resolve_contact_id(self, phone=None, contact_id=None, name=None, create=True):
        """Retorna o contactId a partir de um telefone (busca; se não existir, cria)."""
        if contact_id:
            return contact_id
        contact = None
        try:
            contact = self.get_contact(phone=phone)
        except ZeeError:
            contact = None
        if contact and contact.get("id"):
            return contact["id"]
        if not create:
            return None
        created = self.create_contact(phone, name=name) or {}
        return created.get("id")

    # POST /send-message/{contactId}  body: { text }
    def send_message(self, contact_id, text):
        return self._request("POST", f"/send-message/{contact_id}", json={"text": text})

    def notify_phone(self, phone, text, name=None):
        """Envia uma mensagem de WhatsApp para um número (resolve o contactId antes)."""
        contact_id = self.resolve_contact_id(phone=phone, name=name)
        if not contact_id:
            raise ZeeError(f"Não consegui resolver contactId para o telefone {phone}.")
        return self.send_message(contact_id, text)
