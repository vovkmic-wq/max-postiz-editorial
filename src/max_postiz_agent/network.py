# Проверка URL, ограничение сетевых ответов и запрет приватных адресов.
"""Bounded HTTP collection with DNS pinning, TLS SNI and per-redirect checks."""

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .config import AgentError


# Запрещаем небезопасные схемы, адреса и приватные сети.
def validate_url(url, resolve=True, resolver=None):
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        parsed = urlsplit(url)
        # Обрабатываем ветвь при условии: parsed.scheme not in {'http', 'https'} or not parsed.hostname.
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        # Обрабатываем ветвь при условии: parsed.username or parsed.password or parsed.fragment or (parsed.port not in {None, 80, 443}).
        if (
            parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.port not in {None, 80, 443}
        ):
            raise ValueError
        host = parsed.hostname.rstrip(".").lower()
        # Обрабатываем ветвь при условии: host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')).
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            address = ipaddress.ip_address(host)
            # Обрабатываем ветвь при условии: not address.is_global.
            if not address.is_global:
                raise ValueError
            addresses = [str(address)]
        # Обрабатываем ValueError и выбираем допустимый исход.
        except ValueError:
            # Обрабатываем ветвь при условии: resemblances_ip(host).
            if resemblances_ip(host):
                raise ValueError from None
            addresses = []
        # Обрабатываем ветвь при условии: resolve and (not addresses).
        if resolve and not addresses:
            lookup = resolver or socket.getaddrinfo
            addresses = list(
                {
                    r[4][0]
                    for r in lookup(
                        host,
                        parsed.port or (443 if parsed.scheme == "https" else 80),
                        type=socket.SOCK_STREAM,
                    )
                }
            )
        # Обрабатываем ветвь при условии: resolve and (not addresses or any((not ipaddress.ip_address(a).is_global for a in addresses))).
        if resolve and (
            not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses)
        ):
            raise ValueError
        return host, addresses
    # Обрабатываем (ValueError, OSError) и выбираем допустимый исход.
    except (ValueError, OSError) as exc:
        raise AgentError("SOURCE_URL_FORBIDDEN") from exc


# Операция resemblances_ip: проверка url, ограничение сетевых ответов и запрет приватных адресов.
def resemblances_ip(host):
    # Reject private literal addresses, unusual numeric IPv4 forms and IPv6 after parse failure.
    return ":" in host or host.replace(".", "").isdigit() or host.startswith("0x")


# Нормализуем публичный адрес для поиска повторов.
def canonical_url(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.query, ""))


# Структура SafeHTTP объединяет связанные данные и операции модуля.
class SafeHTTP:
    # Создаём объект и сохраняем зависимости для последующих операций.
    def __init__(self, max_bytes=5242880, redirects=3, transport=None, resolver=None):
        self.max_bytes, self.redirects, self.resolver = max_bytes, redirects, resolver
        self.client = httpx.Client(
            timeout=30, follow_redirects=False, trust_env=False, transport=transport
        )

    # Получаем ответ с учётом сетевых и размерных ограничений.
    def get(self, url, headers=None):
        original_host = None
        headers = dict(headers or {})
        # Последовательно обрабатываем элементы: range(self.redirects + 1).
        for _ in range(self.redirects + 1):
            host, addresses = validate_url(url, resolver=self.resolver)
            # Обрабатываем ветвь при условии: original_host and host != original_host.
            if original_host and host != original_host:
                headers = {
                    k: v for k, v in headers.items() if k.lower() not in {"authorization", "cookie"}
                }
            original_host = host
            original = httpx.URL(url)
            # Connect to the already verified address rather than resolving DNS a second time.
            pinned = original.copy_with(host=addresses[0])
            request_headers = {**headers, "Host": original.netloc.decode()}
            # Выполняем операцию с отдельной обработкой возможных ошибок.
            try:
                # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
                with self.client.stream(
                    "GET", pinned, headers=request_headers, extensions={"sni_hostname": host}
                ) as response:
                    # Обрабатываем ветвь при условии: response.status_code in {301, 302, 303, 307, 308}.
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        # Обрабатываем ветвь при условии: not location.
                        if not location:
                            raise AgentError("SOURCE_BAD_REDIRECT")
                        url = urljoin(url, location)
                        continue
                    # Обрабатываем ветвь при условии: response.status_code in {401, 403, 429}.
                    if response.status_code in {401, 403, 429}:
                        error = AgentError(f"SOURCE_HTTP_{response.status_code}")
                        error.retry_after = response.headers.get("retry-after", "")[:128]
                        raise error
                    # Обрабатываем ветвь при условии: response.status_code == 304.
                    if response.status_code == 304:
                        return 304, b"", dict(response.headers), url
                    # Обрабатываем ветвь при условии: response.status_code != 200.
                    if response.status_code != 200:
                        raise AgentError("SOURCE_HTTP_ERROR")
                    chunks, size = [], 0
                    # Последовательно обрабатываем элементы: response.iter_bytes().
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        # Обрабатываем ветвь при условии: size > self.max_bytes.
                        if size > self.max_bytes:
                            raise AgentError("SOURCE_TOO_LARGE")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    prefix = raw[:65536].lower()
                    if b"<title>captcha" in prefix or b"cf-chl-" in prefix or b"<title>just a moment" in prefix:
                        raise AgentError("SOURCE_CAPTCHA")
                    return 200, raw, dict(response.headers), url
            # Обрабатываем httpx.HTTPError и выбираем допустимый исход.
            except httpx.HTTPError as exc:
                raise AgentError("SOURCE_TRANSPORT_ERROR") from exc
        raise AgentError("SOURCE_REDIRECT_LIMIT")

    # Освобождаем соединения и принадлежащие объекту ресурсы.
    def close(self):
        self.client.close()
