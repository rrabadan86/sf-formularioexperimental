# Integração ZEE -> EVO: cadastro, venda e agendamento de aula experimental.
from .evo_client import EvoClient, EvoError
from .zee_client import ZeeClient, ZeeError
from .orchestrator import (
    book_experimental, BookingResult, TurmaLotadaError, list_alternatives, process_pending,
    available_slots,
)

__all__ = [
    "EvoClient",
    "EvoError",
    "ZeeClient",
    "ZeeError",
    "book_experimental",
    "BookingResult",
    "TurmaLotadaError",
    "list_alternatives",
    "process_pending",
    "available_slots",
]
