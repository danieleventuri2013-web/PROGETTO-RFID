"""Astrazione del trasporto per il lettore Silion SIM7200.

Il modulo SIM7200 e' collegabile in piu' modi:
  - diretto via UART TTL 3.3V (pin del modulo)
  - tramite baseboard SLD1090: USB / RS232 / Ethernet TCP / WiFi / 4G / RS485

Il protocollo Silion e' identico su tutti i trasporti (pacchetti binari con
header 0xFF e CRC-16). Qui si astrae l'I/O grezzo dietro l'interfaccia
`Transport` cosi' il reader e' indipendente dal mezzo fisico.
"""
from __future__ import annotations

import logging
import socket
from abc import ABC, abstractmethod

import serial

log = logging.getLogger("rfid_silion.transport")


class TransportNotOpenError(RuntimeError):
    """I/O richiesto su un trasporto non aperto: chiamare open() prima."""


class Transport(ABC):
    """Interfaccia I/O grezza byte-oriented con timeout."""

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def write(self, data: bytes) -> None: ...

    @abstractmethod
    def read(self, n: int) -> bytes:
        """Legge fino a n byte; ritorna b'' su timeout (nessun dato)."""

    @abstractmethod
    def flush_input(self) -> None: ...

    def describe(self) -> str:
        """Descrizione leggibile del trasporto (per log e health check)."""
        return type(self).__name__


class SerialTransport(Transport):
    """Trasporto seriale (USB-CDC / RS232 / UART tramite adattatore)."""

    def __init__(self, port: str, baudrate: int = 115200,
                 timeout_s: float = 2.0, inter_byte_timeout_s: float = 0.1):
        self.port = port
        self.baudrate = baudrate
        self.timeout_s = timeout_s
        self.inter_byte_timeout_s = inter_byte_timeout_s
        self._ser: serial.Serial | None = None

    def _require_open(self) -> serial.Serial:
        if self._ser is None or not self._ser.is_open:
            raise TransportNotOpenError(
                f"Trasporto seriale {self.port} non aperto: chiamare open()")
        return self._ser

    def open(self) -> None:
        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.timeout_s,
        )
        self._ser.inter_byte_timeout = self.inter_byte_timeout_s
        log.info("Serial open %s @ %d bps", self.port, self.baudrate)

    def close(self) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()
            log.info("Serial closed")

    def write(self, data: bytes) -> None:
        self._require_open().write(data)

    def read(self, n: int) -> bytes:
        return self._require_open().read(n)

    def flush_input(self) -> None:
        self._require_open().reset_input_buffer()

    def describe(self) -> str:
        return f"serial {self.port}@{self.baudrate}"


class TcpTransport(Transport):
    """Trasporto TCP/IP (baseboard SLD1090 via Ethernet/WiFi).

    La SLD1090 espone di default IP 192.168.1.100; il lettore fa da server TCP
    sulla porta 8080 (dal manuale "Basic Steps of Command Development" §1.2).
    Verificare sull'hardware specifico; impostare `tcp.port` in config se diversa.
    """

    def __init__(self, host: str, port: int = 8080, timeout_s: float = 2.0):
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self._sock: socket.socket | None = None

    def _require_open(self) -> socket.socket:
        if self._sock is None:
            raise TransportNotOpenError(
                f"Trasporto TCP {self.host}:{self.port} non aperto: chiamare open()")
        return self._sock

    def open(self) -> None:
        self._sock = socket.create_connection(
            (self.host, self.port), timeout=self.timeout_s)
        self._sock.settimeout(self.timeout_s)
        log.info("TCP open %s:%d", self.host, self.port)

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None
            log.info("TCP closed")

    def write(self, data: bytes) -> None:
        self._require_open().sendall(data)

    def read(self, n: int) -> bytes:
        sock = self._require_open()
        try:
            chunk = sock.recv(n)
        except socket.timeout:
            return b""
        if chunk == b"":
            # recv() == b'' su TCP significa connessione chiusa dal peer,
            # non timeout: segnalarlo aiuta a distinguere i due casi nei log.
            log.warning("TCP: connessione chiusa dal lettore (%s:%d)",
                        self.host, self.port)
        return chunk

    def flush_input(self) -> None:
        sock = self._require_open()
        # imposta timeout nullo per svuotare il buffer
        sock.settimeout(0)
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
        except (BlockingIOError, socket.error):
            pass
        finally:
            sock.settimeout(self.timeout_s)

    def describe(self) -> str:
        return f"tcp {self.host}:{self.port}"
