# Вход владельца в отдельный браузерный профиль проекта.
"""Owner signs in manually in a dedicated project profile; credentials stay in Chrome."""

import argparse
import json
from pathlib import Path

from .browser_runtime import login
from .cli import load_env
from .config import AgentError, Settings


# Разбираем параметры запуска и выбираем сценарий выполнения.
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--chat-id", required=True)
    args = parser.parse_args()
    load_env(args.root)
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        print(json.dumps(login(Settings.load(args.root), args.chat_id)))
    # Обрабатываем AgentError и выбираем допустимый исход.
    except AgentError as exc:
        print(json.dumps({"code": exc.code}))
        raise SystemExit(1) from None


# Обрабатываем ветвь при условии: __name__ == '__main__'.
if __name__ == "__main__":
    main()
