"""Изолированный стандартный диалог выбора; stdout содержит только результат для панели."""

import json
import sys
from pathlib import Path


# Рабочий стол и Tk используются только по нажатию кнопки владельцем на локальном компьютере.
def main():
    window = None
    try:
        import tkinter as tk
        from tkinter import filedialog
        initial, kind, suffixes = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
        window = tk.Tk()
        window.withdraw()
        window.attributes("-topmost", True)
        # Диалог не остаётся висеть после тайм-аута HTTP-операции или ухода пользователя.
        window.after(170000, window.destroy)
        folder = initial if initial.is_dir() else initial.parent
        if kind == "directory":
            path = filedialog.askdirectory(parent=window, title="MAX — выбрать папку проекта",
                                           initialdir=str(folder), mustexist=True)
        else:
            patterns = " ".join("*" + suffix for suffix in suffixes.split())
            path = filedialog.askopenfilename(parent=window, title="MAX — выбрать файл",
                                             initialdir=str(folder),
                                             initialfile=initial.name if initial.is_file() else "",
                                             filetypes=[("Подходящие файлы", patterns)])
        result = {"state": "selected", "path": path} if path else {"state": "cancelled"}
    except Exception:
        # Транспорт не печатает детали исключения и имена приватных системных файлов.
        result = {"state": "unavailable"}
    finally:
        if window is not None:
            try:
                window.destroy()
            except Exception:  # noqa: S110 — уже закрытое окно не требует записи в журнал.
                pass
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
