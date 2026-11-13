# Независимая сверка MAX по установленному выпуску

Начиная с 0.15.7 оба файла сверки включаются в wheel. При каждом выпуске
обязательная проверка wheel испытывает их на собранном пакете, в том числе
отказ при неверном SHA и изменённых установленных файлах. Для нового выпуска
не нужен отдельный манифест исходного дерева. Это не отменяет проверку
фактической доставки в MAX.

Перед использованием получить путь wheel и SHA256 из актуального durable
deployment claim и сверить lifecycle сервиса. Файл контрольных сумм рядом
с wheel сам по себе не заменяет доверенный claim. Использовать байты
проверенного wheel, а не прежние установленные копии.

Из `/opt/max-postiz` запустить `.venv/bin/python -B` с извлечённым
`readback_max.py`, точным record ID и аргументами `--wheel` и
`--wheel-sha256` из claim. `--allow-known-future` допустим лишь для одной
проверенной будущей pending-записи. Перед запуском убедиться, что maintenance,
sender и lease отсутствуют, профиль свободен и до ближайшего due более
300 секунд. Скрипт не назначает слоты, не отправляет и не повторяет доставку.

Пример для VM: подставить **абсолютный** путь и SHA из claim и точный ID
записи. Здесь стандартная библиотека Python сверяет SHA и извлекает оба
скрипта из тех же проверенных байтов в отдельный приватный каталог. При ошибке
извлечения запуск не выполняется. Каталог и отчёт остаются в `data/`.

```bash
(
set -eu
cd /opt/max-postiz
export WHEEL='/абсолютный/путь/к/выпуску.whl'
export WHEEL_SHA='sha256_из_durable_claim'
RECORD='точный-record-id'
CHECK_DIR="$(.venv/bin/python -I -B - <<'PY'
import hashlib, io, os, re, stat, tempfile, zipfile
from pathlib import Path

wheel = Path(os.environ['WHEEL'])
expected = os.environ['WHEEL_SHA']
if (not wheel.is_absolute() or wheel.resolve() != wheel or wheel.is_symlink()
        or not wheel.is_file() or not re.fullmatch(r'[a-f0-9]{64}', expected)
        or wheel.stat().st_size > 100 * 1024 * 1024):
    raise SystemExit('WHEEL_OR_CLAIM_INVALID')
raw = wheel.read_bytes()
if hashlib.sha256(raw).hexdigest() != expected:
    raise SystemExit('WHEEL_SHA256_MISMATCH')
os.umask(0o077)
directory = Path(tempfile.mkdtemp(prefix='readback-wheel-', dir='data/reserve-20261026'))
with zipfile.ZipFile(io.BytesIO(raw)) as archive:
    for name in ('readback_max.py', 'readback_wheel_proof.py'):
        suffix = '.data/data/share/max-postiz/' + name
        matches = [entry for entry in archive.infolist() if entry.filename.endswith(suffix)]
        if (len(matches) != 1 or matches[0].is_dir()
                or stat.S_ISLNK(matches[0].external_attr >> 16)
                or matches[0].file_size > 2 * 1024 * 1024):
            raise SystemExit('WHEEL_READBACK_SCRIPT_INVALID')
        with (directory / name).open('xb') as output:
            output.write(archive.read(matches[0]))
print(directory.resolve())
PY
)"
env -u PYTHONPATH -u PYTHONHOME .venv/bin/python -B "$CHECK_DIR/readback_max.py" \
    "$RECORD" --wheel "$WHEEL" --wheel-sha256 "$WHEEL_SHA"
)
```

Добавить `--allow-known-future` к последней команде только после проверки
единственной будущей pending-записи и безопасного окна.

Скрипт проверяет wheel и установленные файлы пакета до импорта приложения;
его доказательство кода ограничено пакетом `max_postiz_agent`. Он отдельно
сверяет Postiz, две загрузки MAX и сохранённые доказательства. Отчёт и снимки
остаются приватными. `verified` подтверждает содержимое; атрибуция отправителя
требует отдельного доказательства lifecycle и указывается отдельно в выводе.
`check_failed` и код ошибки сохранять как блокер, а не
как подтверждение доставки; не снимать unknown и не повторять отправку.

Хеши 113 исторических записей включены в скрипт: наличие точных записей
проверяется без отдельного приватного файла исторического снимка. Сохранение
всей истории и точных evidence остаётся обязательным. Поддерживаются
реализованные контракты рубрики и представления; новые семантические контракты
требуют отдельной адаптации с проверками. Локальный wheel gate не доказывает
успешную живую сверку MAX или совместимость произвольных будущих изменений API.
