# Configuração via variáveis de ambiente (.env).
# Nada de segredo fica no código: DNS/token do EVO e token do ZEE vêm do ambiente.
import os

from dotenv import load_dotenv

load_dotenv()


def _clean(name: str, default: str = "") -> str:
    val = (os.getenv(name, default) or "").strip()
    # Blindagem: se alguém colou um comentário na mesma linha do .env
    # (ex.: "EVO_BRANCH_ID=    # obrigatório..."), o python-dotenv devolve o
    # comentário como valor. Tratamos um valor que começa com "#" como vazio.
    if val.startswith("#"):
        return default if not default.startswith("#") else ""
    return val


# ================= EVO =================
# Base URL oficial (campo servers[0].url do swagger). Note o "-api" no host.
EVO_BASE_URL = _clean("EVO_BASE_URL", "https://evo-integracao-api.w12app.com.br").rstrip("/")
# Autenticação Basic: usuário = DNS da academia, senha = Secret Key.
EVO_DNS = _clean("EVO_DNS")
EVO_TOKEN = _clean("EVO_TOKEN")
# Filial (opcional; obrigatório apenas em chaves multi-filial).
EVO_BRANCH_ID = _clean("EVO_BRANCH_ID")
EVO_DDI = _clean("EVO_DDI", "55")
EVO_TIMEOUT = int(_clean("EVO_TIMEOUT", "30"))

# Padrões da aula experimental (usados quando não informados na chamada).
# Você pode identificar por NOME (activity/service) ou por ID (idActivity/idService).
EVO_ACTIVITY = _clean("EVO_ACTIVITY")          # nome da atividade, ex.: "SLIMFIT B"
EVO_SERVICE = _clean("EVO_SERVICE")            # nome do serviço "aula experimental"
EVO_ACTIVITY_ID = _clean("EVO_ACTIVITY_ID")    # id da atividade (alternativa ao nome)
EVO_SERVICE_ID = _clean("EVO_SERVICE_ID")      # id do serviço "aula experimental" (ex.: 128)

# Venda do serviço da aula experimental (normalmente gratuito).
EVO_SERVICE_VALUE = float(_clean("EVO_SERVICE_VALUE", "0") or "0")
# Forma de pagamento p/ a venda (EFormaPagamentoTotem).
# Padrão VAZIO = sem cobrança (aula grátis). O valor 1 exige cartão de crédito.
# Se o EVO pedir forma de pagamento, tente EVO_PAYMENT=3 (Dinheiro) ou 4.
EVO_PAYMENT = _clean("EVO_PAYMENT")

# ================= ZEE =================
# A doc do ZEE não trazia o host base explícito; ajuste se necessário.
ZEE_BASE_URL = _clean("ZEE_BASE_URL", "https://magic.zee.tech/api/v1").rstrip("/")
ZEE_TOKEN = _clean("ZEE_TOKEN")
# Autenticação do ZEE: API key no header "z-api-key" (token cru, sem "Bearer").
# São os padrões corretos p/ o ZEE; sobrescreva só se mudar.
ZEE_AUTH_HEADER = _clean("ZEE_AUTH_HEADER", "z-api-key")
ZEE_AUTH_SCHEME = os.getenv("ZEE_AUTH_SCHEME", "").strip()
ZEE_TIMEOUT = int(_clean("ZEE_TIMEOUT", "30"))

# Tags do fluxo automático. ATENÇÃO: o GET /contact do ZEE devolve o NOME da tag
# (não o ID), então filtramos por nome. Para APLICAR a tag (set-contact-tag) usamos o ID.
ZEE_TAG_TODO = _clean("ZEE_TAG_TODO", "FX 3 - Agendou AE")     # nome (filtro)
ZEE_TAG_DONE = _clean("ZEE_TAG_DONE", "FX 4 - Feito")          # nome (filtro)
ZEE_TAG_DONE_ID = _clean("ZEE_TAG_DONE_ID", "a7cf28be-e50f-4a47-b6ed-7ab20d4245b7")  # id (aplicar)
# id da tag "FX 3 - Agendou AE" — depois de marcar "Feito", ela é REMOVIDA do contato.
# Deixe vazio para não remover (só adicionar "Feito", comportamento antigo).
ZEE_TAG_TODO_ID = _clean("ZEE_TAG_TODO_ID", "341fb893-a336-4f60-a675-5383969c7cf9")

# Chaves do metadata do contato de onde ler e-mail e horário escolhido (se a IA salvar lá).
ZEE_META_EMAIL_KEY = _clean("ZEE_META_EMAIL_KEY", "email")
ZEE_META_WHEN_KEY = _clean("ZEE_META_WHEN_KEY", "horario")

# =========== Confirmação para a ALUNA (deve sair pelo WhatsApp do Studio 8550-8065) ===========
# A API do ZEE só envia pelo número dela (996847251), então a confirmação NÃO vai pela ZEE.
# Canal:
#   "outbox" (padrão) = grava a confirmação num arquivo-fila; o bot do Studio (Edge/WhatsApp Web
#                        do 8550-8065) lê e envia. Reusa o sender que você já tem funcionando.
#   "zee"             = envia pela ZEE (sai do 996847251) — use só se quiser mesmo.
#   "off"             = não confirma.
STUDIO_CONFIRM_CHANNEL = _clean("STUDIO_CONFIRM_CHANNEL", "outbox").lower()
# Arquivo-fila (JSON Lines) lido pelo bot do Studio. Cada linha = uma confirmação a enviar.
STUDIO_OUTBOX_FILE = _clean("STUDIO_OUTBOX_FILE", "confirmacoes_outbox.jsonl")
# Placeholders da mensagem: {name} {quando}  (quando = "sexta-feira, 10/07 às 16:15")
ZEE_CONFIRM_TEMPLATE = os.getenv(
    "ZEE_CONFIRM_TEMPLATE",
    "Oie, {name}! 🎉 Tudo bem? Me chamo Juliana e sou do *Studio Slim Fit* do Setor Bueno.\n\n"
    "Estou mandando essa mensagem para informar que a sua aula experimental está confirmada para *{quando}*.\n"
    "Qualquer dúvida que tiver, ou precisar remarcar a sua aula, pode me chamar por aqui.❤️\n\n"
    "*Endereço:* R. C-235, 846, Setor Bueno, Goiânia-GO, 74280-130.\n"
    "*Localização:* https://goo.gl/maps/LFBZhkzbCZ5wJ99f6\n\n"
    "Se possível, tente chegar 10 minutos antes para você conhecer o Studio e conversarmos! 💪\n"
    "Muito bem vinda ao #SlimFit - A Revolução do Treinamento Feminino! ❤️",
)

# =========== Alerta ao Studio (turma lotada / inexistente) — vai pela ZEE (996847251 -> Studio) ===========
ZEE_STUDIO_PHONE = _clean("ZEE_STUDIO_PHONE", "5562985508065")  # destino do alerta (recepção)
ZEE_STUDIO_NAME = _clean("ZEE_STUDIO_NAME", "Studio")
# Placeholders: {name} {phone} {turma} {alternatives}
ZEE_ALERT_LOTADA = os.getenv(
    "ZEE_ALERT_LOTADA",
    "⚠️ {name} ({phone}) tentou agendar numa turma LOTADA ({turma}). "
    "Entrar em contato para solução.{alternatives}",
)
ZEE_ALERT_INEXISTENTE = os.getenv(
    "ZEE_ALERT_INEXISTENTE",
    "⚠️ {name} ({phone}) tentou agendar numa turma INEXISTENTE ({turma}). "
    "Entrar em contato para solução.{alternatives}",
)
# Turma existe e tem vaga, mas o EVO ainda não libera agendar com essa antecedência.
# Cadastro e venda já foram feitos — só falta a recepção matricular manualmente.
ZEE_ALERT_FORA_JANELA = os.getenv(
    "ZEE_ALERT_FORA_JANELA",
    "⚠️ {name} ({phone}) quer a aula experimental em {turma}, mas o EVO ainda não "
    "libera o agendamento com essa antecedência. O cadastro e a venda JÁ foram feitos "
    "— só falta matricular manualmente quando abrir a janela.{alternatives}",
)
