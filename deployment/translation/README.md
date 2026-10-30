# Выделенный перевод EN → RU

**Область документа, 29 октября 2026:** Это инструкция отдельного backend и benchmark переводчиков. В рабочем цикле 0.11.5 используется исправленный MADLAD с exact artifact contract, preflight title/body и независимыми Min/LT. Эксперименты Hy/Argos ниже не переключают автоматический переводчик.
[Текущий выпуск](../../docs/RELEASE_0.11.5.md), [эксплуатация](../../docs/VM_EDITORIAL.md), [навигатор](../../docs/README.md).

Результаты выбора: [сравнительный отчёт](../../docs/TRANSLATION_MODEL_COMPARISON.md).
Рабочий кандидат — `hy-mt2-7b-q8`. Подключение относится к подготовке статей
`HourlyArticleCycle`; legacy editor, Reddit и адаптация вариантов используют свои
прежние маршруты. Конфигурация перевода не включает фоновые задания и публикацию.

## Схема

1. `DedicatedTranslator` разбивает длинный исходник по границам предложений/абзацев,
   сохраняя хвост, и отправляет заголовок и тело отдельно.
2. На VM112 gateway слушает только `127.0.0.1:8094`; он обращается к выбранной
   модели на втором узле `10.20.30.143:8089`.
3. Hy 7B работает на CPU: четыре потока, 4096 контекста, один запрос одновременно.
   Через 15 секунд простоя llama.cpp выгружает модель; следующий запрос включает
   пробуждение. Холодная загрузка с HDD добавляет задержку.
4. Независимый Ministral сохраняет K2200 и прежний API/alias
   `proxmox/qwen25-coder-7b`. Он получает английский исходник вместе с переводом.
5. `ArticleQuality` сохраняет числовой/языковой фильтр и локальный LanguageTool.
   Ошибка, недоступность или отказ проверки блокируют результат. Точный повтор
   отклонённого кандидата не получает нового согласования повторным опросом.

`/health` gateway — проверка его доступности. Она не доказывает готовность модели
или качество перевода; необходим реальный `/translate` и smoke проверки.

## Установка

Используются существующие `/opt/max-postiz` на VM112 и llama.cpp на втором узле.
Версии и SHA моделей закреплены в `models.lock.json`; веса лежат на большом диске
второго узла, не в Git. Загрузка и форматирование не подтверждают качество.

На VM112 отдельное окружение, без изменения зависимостей Hub:

```bash
python3 -m venv /opt/max-postiz/translation-venv
/opt/max-postiz/translation-venv/bin/pip install \
  --extra-index-url https://download.pytorch.org/whl/cpu \
  -r /opt/max-postiz/deployment/translation/requirements.txt
/opt/max-postiz/translation-venv/bin/python \
  /opt/max-postiz/deployment/translation/convert_opus.py
```

Конвертация OPUS воспроизводит INT8-веса; фактический CPU compute type в отчёте —
`int8_float32`. Этот кандидат установлен для экспериментов, основным не выбран.

На втором узле установите `llama-translation-cpu.service` в `/etc/systemd/system/`
и создайте `/etc/default/llama-translation` с абсолютным путём к проверенным весам:

```text
TRANSLATION_MODEL_PATH=/absolute/model/storage/HY-MT2-7B-Q8_0.gguf
TRANSLATION_MODEL_ID=hy-mt2-7b-q8
```

Unit задаёт `CUDA_VISIBLE_DEVICES=-1` и `--device none`: переводчик не занимает
видеопамять рецензента. После `systemd-analyze verify`, `daemon-reload` и запуска
проверьте реальную генерацию, память, OOM и доступность рецензента. Unit допускает
сетевой доступ к модели только с VM112 и самого второго узла.

На VM112 скопируйте `translation-service.example.json` в
`config/translation-service.json`, установите `max-postiz-translation.service`,
выполните `systemd-analyze verify`, `daemon-reload` и запустите gateway.
В `config/translation.json` задайте:

```json
{"enabled": true, "model": "hy-mt2-7b-q8", "base_url": "http://127.0.0.1:8094"}
```

Оба runtime-конфига игнорируются Git. Само обновление переводчика не разрешает
включать задания. По поручению 19 октября рабочий почасовой режим использует
`approval_mode=human`: после Hy и Ministral/LanguageTool создаётся Postiz DRAFT,
финальное согласование делает человек в Hub. Другие маршруты остаются на паузе.
Полная процедура: [ручное согласование](../../docs/HOURLY_ARTICLE_TESTS.md).

На узле с 11,60 GiB RAM оба существующих llama-server используют native
`--sleep-idle-seconds 15`. Для действующего Ministral сохраняется весь исходный
`ExecStart`, добавляется только этот флаг через systemd drop-in; неизвестные
переменные окружения и несуществующий POST `/sleep` не используются. Перед
изменением сохраняют argv/unit и проверяют отсутствие активного inference.

Опциональный `memory_guard` из примера gateway ждёт пассивный
`http://10.20.30.143:8086/props` с `is_sleeping=true` перед единственным Hy-вызовом,
а после него — тот же флаг `http://10.20.30.143:8089/props`. Независимая проверка
не начинается, пока Hy не выгрузился. HTTP-клиент не доверяет proxy environment
и не следует redirects. Чужие запросы на хост guard не блокирует. Ограниченные
ожидания и cold start могут превысить общий клиентский timeout 600 секунд;
это безопасный отказ, а не разрешение повторить неизвестный inference.

## Проверки без публикации

```bash
cd /opt/max-postiz
.venv/bin/python deployment/translation/benchmark_http.py \
  --cases deployment/translation/cases.json --model hy-mt2-7b-q8 \
  --output data/translation-benchmark/http-production.json
.venv/bin/python deployment/translation/smoke_quality.py \
  --root /opt/max-postiz --full-article
.venv/bin/python deployment/translation/check_reference.py
```

Smoke не создаёт Story/Variant/Approval/Dispatch и не вызывает отправку. Он
сохраняет реальные ответы и трассировки в игнорируемой `data/translation-benchmark`.
`check_reference.py` отдельно проверяет заранее сформулированный буквальный
кандидат: его нельзя выдавать за успешный перевод модели. Для сравнения другой
проверяющей модели доступны `--review-model proxmox/qwen35-9b`, `--output-name`
и `--reuse-translation-report`: повторяется оценка той же пары без генерации.
Это диагностика на стенде, которая не переодобряет отклонённые публикации.
Заведомо ложный перевод должен быть отвергнут содержательно: недоступность или
невалидный JSON рецензента считаются неопределённым результатом, не успехом.
Exit 2 означает ошибочное одобрение ложного утверждения; exit 1 — иной отказ или
непройденную проверку; только exit 0 означает прохождение всех запрошенных smoke.

Числовой форматтер восстанавливает исходное написание только при однозначном
полном соответствии значений/количества/знаков. Он не меняет слова, единицы,
факты и смысл; при неопределённости оставляет ответ для обычного отказа gate.
Семантический пропуск или неверная единица остаются задачей независимой проверки.

Для отдельного legacy-сценария возврат к прежнему переводчику задают явным
`enabled=false`; это не переключает почасовой режим с человеческим редактором:
он требует включённый Hy 7B и при его отсутствии отказывает. При ошибке backend
автоматического молчаливого перехода на другую модель нет.

TranslateGemma требует языкового шаблона: `make_gemma_template.py` читает
оригинальный шаблон из GGUF и добавляет адаптацию строковых проб llama.cpp для
фиксированного EN→RU. Проверенный путь рендеринга сохраняет оригинальные
translation instructions. Сервис не предоставляет произвольные языки/чат/инструменты.

## Изолированный стенд Argos Translate

Argos проверяется отдельным CPU-кандидатом и сам по себе не меняет рабочий
переводчик, Hub, расписание или состояние адресатов. Официальный индекс на момент
подготовки стенда содержит прямой пакет `translate-en_ru` версии `1.9`:
[индекс пакетов](https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json),
[архив модели](https://argos-net.com/v1/translate-en_ru-1_9.argosmodel).
Версия Python-библиотеки — `argostranslate==1.11.0`;
[официальные зависимости](https://github.com/argosopentech/argos-translate/blob/v1.11.0/requirements.txt).
Наличие русского языка в индексе не является результатом проверки качества.

На Debian 11 используйте уже доступный Python 3.12, сохранив окружение основного
worker. CPU wheel Torch для CPython 3.12 и manylinux 2.28 совместим с glibc 2.31
Debian 11; устанавливайте его отдельной командой из
[официального CPU-индекса](https://download.pytorch.org/whl/cpu/torch/).
Ни CUDA wheel Torch, ни GPU для этого стенда не требуются.

```bash
ARGOS_ROOT=/srv/dev-disk-by-uuid-8292d473-723d-465f-bb9e-dcd0fb375917/llm-translation/argos
mkdir -p "$ARGOS_ROOT"/share "$ARGOS_ROOT"/cache "$ARGOS_ROOT"/config \
  "$ARGOS_ROOT"/packages "$ARGOS_ROOT"/pip-cache "$ARGOS_ROOT"/tmp
export XDG_DATA_HOME="$ARGOS_ROOT/share"
export XDG_CACHE_HOME="$ARGOS_ROOT/cache"
export XDG_CONFIG_HOME="$ARGOS_ROOT/config"
export ARGOS_PACKAGES_DIR="$ARGOS_ROOT/packages"
export PIP_CACHE_DIR="$ARGOS_ROOT/pip-cache"
export TMPDIR="$ARGOS_ROOT/tmp"
/opt/ozon-worker/venv/bin/python -m venv "$ARGOS_ROOT/venv"
"$ARGOS_ROOT/venv/bin/python" -m pip install \
  --index-url https://download.pytorch.org/whl/cpu 'torch==2.14.1+cpu'
"$ARGOS_ROOT/venv/bin/python" -m pip install \
  'argostranslate==1.11.0' 'ctranslate2==4.8.3' 'sentencepiece==0.2.2' \
  'stanza==1.10.1' 'sacremoses==0.1.1' 'minisbd==0.9.5'
"$ARGOS_ROOT/venv/bin/python" -m pip check
"$ARGOS_ROOT/venv/bin/python" -m pip freeze > "$ARGOS_ROOT/requirements.runtime.txt"
curl --fail --location \
  https://argos-net.com/v1/translate-en_ru-1_9.argosmodel \
  --output "$ARGOS_ROOT/translate-en_ru-1_9.argosmodel"
sha256sum "$ARGOS_ROOT/translate-en_ru-1_9.argosmodel"
"$ARGOS_ROOT/venv/bin/python" -c \
  'import sys; from pathlib import Path; import argostranslate.package as p; p.install_from_path(Path(sys.argv[1]))' \
  "$ARGOS_ROOT/translate-en_ru-1_9.argosmodel"
mkdir -p "$ARGOS_ROOT/minisbd"
curl --fail --location \
  https://github.com/LibreTranslate/MiniSBD/releases/download/v0.0.1/en.onnx \
  --output "$ARGOS_ROOT/minisbd/en.onnx"
sha256sum "$ARGOS_ROOT/minisbd/en.onnx"
```

`ARGOS_PACKAGES_DIR` — официальное имя переменной во множественном числе.
XDG data/cache/config задаются раздельно: веса, NLP-ресурсы, временные загрузки и
pip cache остаются на большом диске. Остальные зависимости Argos, включая SpaCy
и ONNX Runtime, разрешаются pip и фиксируются в runtime freeze; их установка ещё не
доказывает успешный инференс. Вычисленный SHA архива фиксирует скачанный артефакт;
в официальном JSON индекса отдельной SHA-256 суммы для EN→RU нет.
Скачанный и проверенный ZIP зафиксирован в `argos-model.lock.json`: 195 746 693
байта, SHA-256
`591d743ae103752b88ffc38785c50421320f4eff933c8967e0d3d2e14d4e27811`.
Эта сумма получена локально и не обозначает подпись издателя модели.
В том же manifest закреплён отдельный английский sentence detector MiniSBD:
188 043 байта, SHA-256
`6fa9f3a3b201687bd43e329b2b0736789efc97b37883b5b759b31988dca9f353`.
ONNX скачивается один раз на этапе подготовки; файл не добавляется внутрь
исходного пакета Argos.

После установки скопируйте `benchmark_argos.py`, тот же `cases.json` и, при
необходимости, приватный JSON полной статьи на узел. Результат сохраняйте в
игнорируемом каталоге данных:

```bash
"$ARGOS_ROOT/venv/bin/python" /opt/translation-lab/benchmark_argos.py \
  --cases /opt/translation-lab/cases.json \
  --data-dir "$ARGOS_ROOT" --packages-dir "$ARGOS_ROOT/packages" \
  --sbd-model "$ARGOS_ROOT/minisbd/en.onnx" \
  --package-version 1.9 \
  --model-archive "$ARGOS_ROOT/translate-en_ru-1_9.argosmodel" \
  --full-source /opt/translation-lab/article-20261018-5503.local.json \
  --output /opt/translation-lab/results/argos.json
```

`--full-source` необязателен; берётся всё поле `original` без обрезания, ID случая
`nasa-escapade-full`. Основной корпус проходит один раз, последние два случая —
повторно; прогрев хранится отдельно и не входит в медиану. Runner использует
`PackageTranslation` напрямую, обходя `CachedTranslation`, поэтому повторный
замер выполняет инференс. Отчёт сохраняет исходные metadata пакета, SHA каждого
файла и архива, версии, фактический compute type, загрузку, RSS, исходник,
перевод, ошибки и реальные границы предложений, использованные Argos.

Стенд задаёт CPU, два intra-потока, один inter-поток, INT8 и скрывает CUDA.
Argos `ARGOS_CHUNK_TYPE=MINISBD` использует отдельно подготовленный английский
ONNX detector. До первого перевода runner создаёт
`SBDetect(absolute_local_path, use_gpu=False, max_threads=2)` и передаёт экземпляр
штатному `MiniSBDSentencizer` Argos. Это официальный локальный путь MiniSBD,
согласно [документации](https://github.com/LibreTranslate/MiniSBD/tree/v0.9.5)
и [API закреплённой версии](https://github.com/LibreTranslate/MiniSBD/blob/v0.9.5/minisbd/minisbd.py).
В отчёте фиксируются SHA ONNX, фактический provider и intra-op threads;
любое значение provider, кроме `CPUExecutionProvider`, и число потоков, отличное
от двух, блокируются. Дополнительно runner блокирует Python socket connect/DNS
через audit hook. Отсутствующий или пустой `--sbd-model` приводит к ошибке до
импорта моделей. Все sentence boundaries берутся из реальных вызовов detector,
а исходные переводные веса и файлы пакета остаются прежними.

Упакованный в Argos EN→RU 1.9 старый Stanza tokenizer несовместим с Stanza
1.10.1. Две предварительные попытки завершились до инференса: `KeyError:
'packages'` при чтении индекса и `KeyError: 'feat_dropout'` после адаптации
только схемы индекса. Эти отчёты сохраняются отдельно как неуспешные; их нельзя
считать тестами качества перевода. Runner больше не адаптирует этот checkpoint
и не загружает его. MiniSBD — явно другой sentence detector, его выбор и SHA
фиксируются для честного воспроизведения. Не заменяйте это
`ARGOS_CHUNK_TYPE=NONE`: в Argos 1.11.0 этот вариант ещё не реализован.

## Непубликующий тест каскада

Скопируйте завершённый `argos.json` в приватную `data/translation-benchmark/`
на VM112. Один проверяющий получает только английский исходник и русский
кандидат; ему не передаются другой перевод, эталон, название переводчика
или решение предыдущей проверки. Основной прогон:

```bash
.venv/bin/python deployment/translation/benchmark_cascade.py \
  --root /opt/max-postiz \
  --argos-results data/translation-benchmark/argos.json \
  --output-name cascade-argos-hy7b.json --controls --full-article
```

Семь уникальных случаев оцениваются по одному разу; два повтора Argos служат
только для скорости. Полное тело NASA проверяется отдельно от заголовка.
Перед корпусом выполняются буквальный положительный и заведомо ложный
контроль. Отклонение, отсутствие проверки или несовпадение числового multiset
вызывают один Hy fallback. Его повторное отклонение или непроверенность требуют
редактора. Согласие двух переводчиков не является условием принятия.

Для продолжения используйте `--resume` с теми же входами и промптом.
Неизвестный исход не повторяется; точный прежний отказ не пересэмплируется.
`approved` означает решение модели в эксперименте, а не доказанную точность
и не согласование публикации. Полный производственный ArticleQuality/
LanguageTool gate этим fragment-стендом не заменяется.

Дополнительное исследование после завершения первого прогона:

```bash
.venv/bin/python deployment/translation/benchmark_cascade_complex.py \
  --root /opt/max-postiz \
  --argos-results data/translation-benchmark/argos.json \
  --seed-report data/translation-benchmark/cascade-argos-hy7b.json \
  --output-name cascade-complex-argos-hy7b.json \
  --complexity-policy length-acronyms-v1 --controls --full-article
```

Длина исходника от 350 символов или английская аббревиатура из трёх заглавных
букв принудительно выбирают Hy. Совпадающие переводы и проверки берутся из
зафиксированного первого отчёта; новые вызовы и их стоимость отмечены отдельно.
Правило добавлено после обнаружения ложного одобрения: это исследовательское
усиление, не слепой сравнительный тест и не доказательство качества эвристики.

Оба CLI сохраняют только приватные отчёты, не создают статьи, согласования
или отправки и не меняют рабочий маршрут Hy/паузы. Результаты, ограничения
формата замечаний и смешанного профиля памяти описаны в
[отчёте](../../docs/ARGOS_CASCADE_EVALUATION.md).

## Causal-позиции MADLAD в Candle

Для MADLAD-400-3B на pinned Candle
`12bb5d1f6350f4ea9827d9086f213feec1f0af6a` необходим
[патч](candle-t5-causal-position.patch): исходный quantized T5 применяет
двунаправленные relative-position buckets также к декодеру. Эталонный
[Transformers T5](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/t5/modeling_t5.py)
различает энкодер и декодер. При расстоянии 16 исходный decoder выбирает bucket
10 вместо 16; это влияет на inference и делает старые замеры этого backend
непригодными для оценки качества MADLAD.

Применяйте патч из корня отдельной копии Candle, сохранив исходную сборку:

```bash
git apply --check /path/to/max-postiz/deployment/translation/candle-t5-causal-position.patch
git apply /path/to/max-postiz/deployment/translation/candle-t5-causal-position.patch
cargo test --locked --release -p candle-transformers --lib max_postiz_bucket_contract --no-default-features
```

Патч проверяет 1026 значений по формуле эталона и отдельный causal-контрпример.
После сборки фиксируйте новый SHA бинарника и отдельный профиль эксперимента.
Прежние raw и отказы сохраняются; их нельзя переписать успешными ответами новой
сборки. Это исправление изолированного стенда, рабочий переводчик не переключается.
