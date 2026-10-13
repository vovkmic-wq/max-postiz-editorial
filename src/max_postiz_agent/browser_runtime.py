# Блокировки профиля, запуск браузера и проверка адресата.
"""Dedicated browser profile and public MAX DOM; no cookie export or hidden API access."""

import os
import re
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

from .config import AgentError, inside


ORIGIN = "https://web.max.ru"
COMPOSER = '[contenteditable][role="textbox"][aria-placeholder="Пост"]'


# Удерживаем системную блокировку профиля на время работы с браузером.
@contextmanager
def profile_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        # Обрабатываем ветвь при условии: not handle.tell().
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            # Обрабатываем ветвь при условии: os.name == 'nt'.
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                # Альтернативная ветвь после проверки основного условия.
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Обрабатываем OSError и выбираем допустимый исход.
        except OSError as exc:
            raise AgentError("BROWSER_PROFILE_BUSY", 409) from exc
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            yield
        finally:
            # Завершаем работу и освобождаем ресурсы независимо от результата.
            handle.seek(0)
            # Обрабатываем ветвь при условии: os.name == 'nt'.
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                # Альтернативная ветвь после проверки основного условия.
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# Разрешаем только адресата из локального списка каналов.
def channel_name(settings, chat_id):
    # Обрабатываем ветвь при условии: not re.fullmatch('-?\\d+', chat_id).
    if not re.fullmatch(r"-?\d+", chat_id):
        raise AgentError("BROWSER_CHANNEL_NOT_ALLOWED")
    name = settings.app["max"].get("browser", {}).get("channels", {}).get(chat_id)
    # Обрабатываем ветвь при условии: not isinstance(name, str) or not name.strip().
    if not isinstance(name, str) or not name.strip():
        raise AgentError("BROWSER_CHANNEL_NOT_ALLOWED")
    return name


# Открываем ограниченный контекст работы и гарантируем освобождение ресурсов.
@contextmanager
def session(settings, *, visible=False):
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        from playwright.sync_api import sync_playwright
    # Обрабатываем ImportError и выбираем допустимый исход.
    except ImportError as exc:
        raise AgentError("BROWSER_DEPENDENCY_REQUIRED") from exc
    options = settings.app["max"].get("browser", {})
    external = os.getenv("MAX_EXISTING_BROWSER_PROFILE", "")
    if external:
        from pathlib import Path
        profile = Path(external).resolve()
        if not profile.is_dir() or profile.name != "max-browser" or profile.parent.name != "data":
            raise AgentError("BROWSER_EXTERNAL_PROFILE_INVALID")
    else:
        profile = inside(settings.root, options.get("profile_dir", "data/max-browser"))
    media = inside(settings.root, settings.app["media_dir"])
    # Обрабатываем ветвь при условии: not profile.is_relative_to(settings.root / 'data') or profile == settings.root / 'data' or profile.is_relat….
    if (not external and (not profile.is_relative_to(settings.root / "data") or profile == settings.root / "data")) or profile.is_relative_to(media) or media.is_relative_to(profile):
        raise AgentError("BROWSER_PROFILE_INVALID")
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with profile_lock(profile.with_suffix(".lock")), sync_playwright() as playwright:
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            context = playwright.chromium.launch_persistent_context(
                str(profile), channel=options.get("channel", "chrome"),
                headless=False if visible else options.get("headless", True),
                chromium_sandbox=True, locale="ru-RU", viewport={"width": 1280, "height": 900},
                accept_downloads=False,
            )
        # Обрабатываем Exception и выбираем допустимый исход.
        except Exception as exc:
            raise AgentError("BROWSER_START_FAILED") from exc
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(15000)
            yield page
        finally:
            # Завершаем работу и освобождаем ресурсы независимо от результата.
            context.close()


# Сверяем URL, точное название и доступ к целевому каналу.
def ensure_channel(page, settings, chat_id, *, navigate=True):
    expected = channel_name(settings, chat_id)
    # Обрабатываем ветвь при условии: navigate.
    if navigate:
        page.goto(ORIGIN + "/" + chat_id, wait_until="domcontentloaded")
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        heading = page.locator("#main-header-title")
        heading.filter(has_text="Окно чата с " + expected).wait_for(state="attached")
        # Обрабатываем ветвь при условии: heading.inner_text().strip() != 'Окно чата с ' + expected.
        if heading.inner_text().strip() != "Окно чата с " + expected:
            raise AgentError("BROWSER_WRONG_CHANNEL")
    # Обрабатываем AgentError и выбираем допустимый исход.
    except AgentError:
        raise
    # Обрабатываем Exception и выбираем допустимый исход.
    except Exception as exc:
        raise AgentError("BROWSER_LOGIN_OR_CHANNEL_REQUIRED") from exc
    parsed = urlsplit(page.url)
    # Обрабатываем ветвь при условии: parsed.scheme != 'https' or parsed.hostname != 'web.max.ru' or parsed.path != '/' + chat_id.
    if parsed.scheme != "https" or parsed.hostname != "web.max.ru" or parsed.path != "/" + chat_id:
        raise AgentError("BROWSER_WRONG_CHANNEL")
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        page.locator(COMPOSER).wait_for(state="visible")
    # Обрабатываем Exception и выбираем допустимый исход.
    except Exception as exc:
        raise AgentError("BROWSER_PUBLISH_PERMISSION_REQUIRED") from exc
    # Обрабатываем ветвь при условии: not page.locator(COMPOSER).is_visible().
    if not page.locator(COMPOSER).is_visible():
        raise AgentError("BROWSER_PUBLISH_PERMISSION_REQUIRED")


# Находим сообщения с точным текстом для сверки результата.
def matching_posts(page, text):
    return page.get_by_role("listitem").filter(has=page.get_by_text(text, exact=True))


# Открываем отдельный профиль для ручного входа владельца.
def login(settings, chat_id, timeout=900):
    expected = channel_name(settings, chat_id)
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with session(settings, visible=True) as page:
        page.goto(ORIGIN + "/" + chat_id, wait_until="domcontentloaded")
        print("MAX_BROWSER_LOGIN_REQUIRED", flush=True)
        deadline = time.monotonic() + timeout
        opened_channel = False
        # Продолжаем цикл, пока выполняется условие: time.monotonic() < deadline.
        while time.monotonic() < deadline:
            # Выполняем операцию с отдельной обработкой возможных ошибок.
            try:
                heading = page.locator("#main-header-title")
                # Обрабатываем ветвь при условии: heading.count() and heading.inner_text().strip() == 'Окно чата с ' + expected.
                if heading.count() and heading.inner_text().strip() == "Окно чата с " + expected:
                    ensure_channel(page, settings, chat_id, navigate=False)
                    return {"state": "available", "chat_id": chat_id}
                # Обрабатываем ветвь при условии: not opened_channel and page.get_by_role('button', name='Настройки', exact=True).count().
                if not opened_channel and page.get_by_role("button", name="Настройки", exact=True).count():
                    page.goto(ORIGIN + "/" + chat_id, wait_until="domcontentloaded")
                    opened_channel = True
            # Обрабатываем AgentError и выбираем допустимый исход.
            except AgentError:
                raise
            # Обрабатываем Exception и выбираем допустимый исход.
            except Exception:
                # Navigation during a manual sign-in can briefly replace the document.
                # Обрабатываем ветвь при условии: page.is_closed().
                if page.is_closed():
                    raise AgentError("BROWSER_WINDOW_CLOSED") from None
                time.sleep(0.1)
            time.sleep(2)
        raise AgentError("BROWSER_LOGIN_TIMEOUT")
