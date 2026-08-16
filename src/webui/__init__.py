"""Interfaccia operativa web locale.

Sta sopra il contratto del servizio (`RFIDService` / `RFIDRPCDispatcher`) e sopra
il flusso di laboratorio (`lims.*`). Come `src/lims/`, **non importa mai**
`reader`, `protocol` o `transports`.
"""

from .server import STATIC_DIR, EventBus, WebUIServer
from .workflow import Workflow, WorkflowError

__all__ = ["WebUIServer", "EventBus", "STATIC_DIR", "Workflow", "WorkflowError"]
