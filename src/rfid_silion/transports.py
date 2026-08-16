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
from contextlib import contextmanager
from typing import Iterator

import serial

log = logging.getLogger("rfid_silion.transport")


class TransportError(RuntimeError):
    """Errore I/O normalizzato del livello di trasporto."""


class TransportNotOpenError(TransportError):
    """I/O richiesto su un trasporto non aperto: chiamare open() prima."""


class TransportDisconnectedError(TransportError):
    """Il dispositivo o il peer TCP ha chiuso la connessione."""


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

    def set_timeout(self, timeout_s: float) -> None:
        """Aggiorna il timeout del trasporto, anche quando e' gia' aperto."""
        if timeout_s <= 0:
            raise ValueError(f"timeout_s deve essere > 0: {timeout_s}")
        self.timeout_s = timeout_s

    @contextmanager
    def temporary_timeout(self, timeout_s: float | None) -> Iterator[None]:
        """Applica temporaneamente un timeout; None lascia quello corrente."""
        if timeout_s is None:
            yield
            return
        had_timeout = hasattr(self, "timeout_s")
        previous = getattr(self, "timeout_s", None)
        self.set_timeout(timeout_s)
        try:
            yield
        finally:
            if had_timeout and previous is not None:
                self.set_timeout(previous)
            elif not had_timeout:
                del self.timeout_s

    def describe(self) -> str:
        """Descrizione leggibile del trasporto (per log e health check)."""
        return type(self).__name__


class SerialTransport(Transport):
    """Trasporto seriale (USB-CDC / RS232 / UART tramite adattatore)."""

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        timeout_s: float = 2.0,
        inter_byte_timeout_s: float = 0.1,
    ):
        self.port = port
        self.baudrate = baudrate
        self.timeout_s = timeout_s
        self.inter_byte_timeout_s = inter_byte_timeout_s
        self._ser: serial.Serial | None = None

    def _require_open(self) -> serial.Serial:
        if self._ser is None or not self._ser.is_open:
            raise TransportNotOpenError(
                f"Trasporto seriale {self.port} non aperto: chiamare open()"
            )
        return self._ser

    def open(self) -> None:
        try:
            port = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout_s,
            )
            port.inter_byte_timeout = self.inter_byte_timeout_s
        except (serial.SerialException, OSError) as e:
            self._ser = None
            raise TransportError(f"Impossibile aprire la seriale {self.port}: {e}") from e
        self._ser = port
        log.info("Serial open %s @ %d bps", self.port, self.baudrate)

    def close(self) -> None:
        port, self._ser = self._ser, None
        if port and port.is_open:
            try:
                port.close()
            except (serial.SerialException, OSError) as e:
                raise TransportError(f"Errore chiusura seriale {self.port}: {e}") from e
            log.info("Serial closed")

    def write(self, data: bytes) -> None:
        try:
            self._require_open().write(data)
        except (serial.SerialException, OSError) as e:
            raise TransportError(f"Errore scrittura seriale {self.port}: {e}") from e

    def read(self, n: int) -> bytes:
        try:
            return self._require_open().read(n)
        except (serial.SerialException, OSError) as e:
            raise TransportError(f"Errore lettura seriale {self.port}: {e}") from e

    def flush_input(self) -> None:
        try:
            self._require_open().reset_input_buffer()
        except (serial.SerialException, OSError) as e:
            raise TransportError(f"Errore flush seriale {self.port}: {e}") from e

    def set_timeout(self, timeout_s: float) -> None:
        super().set_timeout(timeout_s)
        if self._ser is not None and self._ser.is_open:
            self._ser.timeout = timeout_s

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
                f"Trasporto TCP {self.host}:{self.port} non aperto: chiamare open()"
            )
        return self._sock

    def open(self) -> None:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
            sock.settimeout(self.timeout_s)
        except OSError as e:
            self._sock = None
            raise TransportDisconnectedError(
                f"Impossibile aprire TCP {self.host}:{self.port}: {e}"
            ) from e
        self._sock = sock
        log.info("TCP open %s:%d", self.host, self.port)

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock:
            try:
                sock.close()
            except OSError as e:
                raise TransportDisconnectedError(
                    f"Errore chiusura TCP {self.host}:{self.port}: {e}"
                ) from e
            log.info("TCP closed")

    def write(self, data: bytes) -> None:
        try:
            self._require_open().sendall(data)
        except OSError as e:
            raise TransportDisconnectedError(
                f"Errore scrittura TCP {self.host}:{self.port}: {e}"
            ) from e

    def read(self, n: int) -> bytes:
        sock = self._require_open()
        try:
            chunk = sock.recv(n)
        except socket.timeout:
            return b""
        except OSError as e:
            raise TransportDisconnectedError(
                f"Errore lettura TCP {self.host}:{self.port}: {e}"
            ) from e
        if chunk == b"":
            raise TransportDisconnectedError(
                f"Connessione TCP chiusa dal lettore {self.host}:{self.port}"
            )
        return chunk

    def flush_input(self) -> None:
        sock = self._require_open()
        # imposta timeout nullo per svuotare il buffer
        sock.settimeout(0)
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    raise TransportDisconnectedError(
                        f"Connessione TCP chiusa dal lettore {self.host}:{self.port}"
                    )
        except BlockingIOError:
            pass
        except TransportDisconnectedError:
            raise
        except OSError as e:
            raise TransportDisconnectedError(
                f"Errore flush TCP {self.host}:{self.port}: {e}"
            ) from e
        finally:
            try:
                sock.settimeout(self.timeout_s)
            except OSError:
                pass

    def set_timeout(self, timeout_s: float) -> None:
        super().set_timeout(timeout_s)
        if self._sock is not None:
            try:
                self._sock.settimeout(timeout_s)
            except OSError as e:
                raise TransportDisconnectedError(
                    f"Impossibile impostare timeout TCP {self.host}:{self.port}: {e}"
                ) from e

    def describe(self) -> str:
        return f"tcp {self.host}:{self.port}"
