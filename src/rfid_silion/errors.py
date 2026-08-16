"""Codici di stato Silion -> eccezioni.

Tabella da MANUALI/Communication_Protocol_Doc__20210716/html/Status_Code.html
"""

from __future__ import annotations


class SilionError(Exception):
    """Errore generico del lettore Silion."""

    def __init__(self, status: int, message: str = ""):
        self.status = status
        self.message = message or STATUS_MESSAGES.get(status, f"Unknown status 0x{status:04X}")
        super().__init__(f"0x{status:04X}: {self.message}")


STATUS_MESSAGES = {
    0x0000: "Operation successful",
    0x0100: "Data length mismatch",
    0x0101: "Unavailable command (not in App firmware? run Boot Firmware 0x04)",
    0x0105: "Unavailable parameter value",
    0x010A: "Unavailable baud rate",
    0x010B: "Unavailable region selection",
    0x0200: "App Firmware CRC incorrect",
    0x0302: "Flash write failed",
    0x0400: "No tag found",
    0x0402: "Protocol unavailable",
    0x0404: "Embedded operation: write OK but read-back failed",
    0x040A: "General tag error (read/write lock, kill)",
    0x040B: "Read memory length out of limit (max 96 words)",
    0x040C: "Unavailable kill password",
    0x0420: "GEN2 protocol error",
    0x0423: "Memory overrun / bad PC",
    0x0424: "Memory locked",
    0x042B: "Insufficient power",
    0x042F: "Non specific error",
    0x0430: "Unknown error",
    0x0500: "Unavailable frequency value",
    0x0504: "Temperature overrun",
    0x0505: "High return loss (antenna problem)",
    0x7F00: "Serious unknown error",
    # Codici di init firmware EX10 (manuale 2024-12 / 2023-05-30).
    0xFF11: "FLASH init error",
    0xFF12: "GPIO init error",
    0xFF13: "Timer init error",
    0xFF14: "SPI init error",
    0xFF15: "Antenna control init error",
    0xFF16: "Band init error",
    0xFF17: "EX10 init error",
    0xFF1F: "Firmware abnormal",
    0xFFFF: "Hardware version wrong",
    # NB: i comandi estesi (framing 0xAA) e altri codici 0xEExx / 0xAAxx / 0x500F
    # / 0x50FF esistono nel manuale ma non sono ancora mappati qui; verranno
    # mostrati come "Unknown status 0x....".
}


class NoTagError(SilionError):
    """0x0400 - nessun tag nel campo. Non fatale per l'inventario."""


_EXC_MAP = {
    0x0400: NoTagError,
}


def status_to_exception(status: int) -> type:
    return _EXC_MAP.get(status, SilionError)


def check_status(status: int) -> None:
    """Solleva l'eccezione appropriata se status != 0."""
    if status == 0x0000:
        return
    raise status_to_exception(status)(status)
