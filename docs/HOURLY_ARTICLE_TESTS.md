# Почасовой автоматический цикл MAX–Postiz

Версия 0.6.24, 21 октября 2026, Europe/Moscow. После демонстрации ручного процесса
владелец отменил обязательного редактора-человека для max_test и поручил вернуть
автоматическую публикацию со смягчённой машинной проверкой. Режим human сохранён
для истории и других явных сценариев, но новая политика использует automatic.

## Цепочка

1. Desktop heartbeat выбирает один новый разрешённый текст NASA SVS, проверяет
   оригинальную страницу, narrative, credits и права. Штатный fetch сохраняет
   полный текст и дату, без изображений/видео. URL и исходники, включая отменённые,
   проверяются на повторы.
   До вызова моделей также сверяют реальную дату release с текущим
   max_source_age_hours из политики; сейчас это 8760 часов. STALE_SOURCE
   не разрешает подменять дату или увеличивать лимит. Если отказ получен
   до claim, отсутствие записи и занятого часа проверяется по ledger.
2. Hy-MT2-7B Q8 переводит EN → RU через dedicated gateway. Отдельный Ministral
   сравнивает оригинал и перевод по версии критериев meaning-first-v1.
   LanguageTool проверяет русский текст. Qwen/Argos не заменяют этот маршрут.
   Inline Markdown-ссылки переводятся в обычный текст: подпись остаётся в
   предложении, буквальный URL переносится на отдельную строку до всех проверок.
   Финальная проверка и хеши относятся именно к этому тексту для MAX.
   С версии 0.6.24 загрузчик сохраняет также HTTP(S) href HTML-ссылок narrative:
   подпись и адрес поступают в article-links-v1, а не исчезают при очистке HTML.
   Сверяются все видимые слова и адреса; небезопасная ссылка блокирует загрузку.
3. Успешный результат создаёт один Postiz DRAFT и точные машинные доказательства.
   Отдельная операция политики назначает будущую дату тому же посту и фиксирует
   approval от hourly_article_policy для exact revision/hash/target/date.
   Это не человеческое согласование и не нажатие checkbox от имени владельца.
4. Существующий единственный Hub worker --dispatch-only отправляет запись в MAX
   не раньше даты и не раньше 3600 секунд после предыдущей фактической доставки.
   Успех подтверждают verified dispatch, Postiz PUBLISHED и точный текст канала.
   После подтверждения max_test снова paused до следующей отдельной отправки.

## Смягчённые критерии

| Не блокируют | Блокируют |
|---|---|
| Стилистическое предпочтение, менее изящный заголовок, понятная буквальность | Изменённый/выдуманный факт, существенный пропуск |
| Верный синоним или научный термин с тем же смыслом | Потеря отрицания, осторожности may/could, неверная причинность |
| Необязательная типографика, строго распознанный LT style/register | Числа, единицы, ссылки, неподходящий язык, орфография/грамматика |
| Замечание о факте, уже переданном в другом предложении | Неустановленные права, запретная тема, невалидный verdict, сбой сервиса |

Ministral возвращает approved/topic/issues/warnings. Только существенные ошибки
идут в issues; warnings сохраняются отдельно. Negative не переименовывается в
positive, прежний refusal не удаляется. Exact EN/RU, версия рубрики и hash промпта
связаны с доказательствами. Процент прохождения нельзя обещать без репрезентативной
живой выборки; тесты контракта не доказывают смысловую точность модели.

## Эксплуатация

Подготовка запускается desktop heartbeat и требует работающего компьютера с Codex.
Сон/гибернация компьютера останавливают этот этап, даже если VM и модели доступны.
Для рабочего места, постоянно подключённого к сети, предусмотрена обратимая
[защита от сна](WORKSTATION_POWER.md). Она меняет только AC-параметры; работа
от батареи и настройки безопасности сохраняются. Это не подтверждение защиты
от выключения, закрытия приложения, сбоя сети или отключения питания.
После длительного перерыва прежний час и неизвестные исходы не сбрасываются:
сначала выполняется сверка, затем допускается только один текущий цикл.

VM: editorial@10.20.30.45, /opt/max-postiz. Строгий SSH использует
data/linux-vm_ed25519 и data/linux-vm-known-hosts.

    .venv/bin/python scripts/hourly_article_test.py status
    .venv/bin/python scripts/fetch_nasa_article.py https://svs.gsfc.nasa.gov/ID/ --output data/article.local.json
    .venv/bin/python scripts/hourly_article_test.py prepare data/article.local.json
    .venv/bin/python scripts/hourly_article_test.py publish RECORD_ID --reviewed-hash HASH --due-at UNIX_TIMESTAMP

ID, RECORD_ID, HASH и будущую дату берут из проверенного материала и актуальной
записи. До publish обязательны точный source, новая машинная проверка, свободный
интервал, отсутствие sending/unknown/lease и совпадение текста Postiz.
data/hourly-article-policy.local.json содержит enabled=true,
approval_mode=automatic, review_rubric=meaning-first-v1. Пример в config выключен.
Новая политика явно фиксирует поручение владельца. Права NASA SVS проверяются
по [условиям](https://svs.gsfc.nasa.gov/help/) и конкретной странице.

Старые human-черновики сохраняются. Адресная миграция одной записи запускает одну
оценку по новой рубрике и сохраняет прежние metadata, raw/parsed отказ, traces,
даты и использованный час. Она не согласует и не отправляет запись. Неизменённый
отказ одной версии рубрики повторно не опрашивают; неизвестный исход миграции или
внешнего scheduling не повторяют. Массовой миграции/восстановления нет.

Для одной прежней записи используется команда с обязательной причиной:

    .venv/bin/python scripts/hourly_article_test.py migrate-automatic RECORD_ID --reason "Owner changed the approval policy and requested one new meaning-first review"

Если уже есть automatic_ready, следующий свободный час используют для одной
существующей записи вместо новой статьи. Подготовка может пересечь границу часа:
publish резервирует один automatic_slot текущего часа, после чего новая prepare
в этом же часу запрещена. Это не разрешает повтор sending/unknown или перенос
просроченного dispatch. Отклонённый/инфраструктурно неуспешный результат блокируется.
До DRAFT реальную ошибку исправляют аудируемым prepare --retry-record --candidate.
Локальный revise-prepared для уже staged automatic запрещён: редактирование
того же поста выполняется в Postiz, прежняя проверка инвалидируется. Отправлять
его без нового рабочего пути машинной проверки нельзя.

Не более одной новой статьи за час, до 24 ожидающих записей, одна фактическая
доставка за 3600 секунд. Пропуски не догоняются. Просрочка dispatch более часа
требует отдельного решения, а не автоматической пересылки. Другие маршруты,
Reddit, сборщики, recurring и каскад Argos этим поручением не включены.
Postiz запускает только main/max processors, RUN_CRON пуст. Второй worker и широкий
tick запрещены. Правки текста/даты/адресата инвалидируют прежнее согласование.

Для подтверждённого automatic hourly лимиты планирования берутся из связанной
политики (минимум 3600 секунд, максимум 24 в сутки, Europe/Moscow), а не из старого
профиля с интервалом 120 минут. Другие маршруты и прежний human-режим сохраняют
лимиты профиля. Та же проверка интервала и суток выполняется до изменения Postiz
и повторно при согласовании; фактическая доставка дополнительно защищена cadence.

Если Postiz уже подтвердил exact QUEUE, но approval детерминированно остановлен
SCHEDULE_GAP_TOO_SHORT, существует отдельный адресный reconcile-approval с текущим
reviewed-hash, неизменной будущей датой и причиной. После полного сравнения remote
и local, отсутствия любых прежних approvals/dispatches/lease и проверки актуальных
доказательств он фиксирует одноразовый durable claim и выполняет только approval.
Повторных Postiz POST и вызовов моделей нет. Unknown, иная ошибка, stale evidence,
изменение текста/адресата/даты или прошлое время блокируются. Claim не сбрасывается;
после неизвестного исхода сначала сверяют DB и ledger, повтор команды запрещён.

    .venv/bin/python scripts/hourly_article_test.py reconcile-approval RECORD_ID --reviewed-hash CURRENT_HASH --due-at SAME_FUTURE_UNIX_TIMESTAMP --reason "Known scheduling succeeded; reconcile exact pending approval after fixing the profile-policy conflict"

Эта команда не заменяет publish и не применима к произвольным отказам. CURRENT_HASH
берут из свежего status после сверки того же QUEUE; SAME_FUTURE_UNIX_TIMESTAMP
должен совпадать с уже сохранённой датой. При existing claim команда останавливается.

С 0.6.19 advisory warnings имеют отдельную ограниченную вместимость: до 8 строк,
каждая до 1024 символов, суммарно до 8192 UTF-8 байт. Содержательные issues
по-прежнему ограничены тремя; отрицательное решение и ошибки типов блокируют.
Ни raw, ни замечания не усекаются. Семантический промпт и meaning-first-v1
сохранены: желательная краткость ответа не меняет порог фактической точности.

Для известного сбоя исключительно старого количества warnings (4..8) существует
отдельный recover-review-protocol. Нужны последний полный trace с его SHA256,
точный input/candidate/source/prompt/policy, положительный strict typed verdict
без issues и отсутствие любых следов staging/approval/dispatch/lease. Старый
review_invalid остаётся в истории; одноразовый durable claim не снимается.
Операция не вызывает модели: исходный verdict и все warnings используются
неизменными, затем выполняются hard gates и новый LanguageTool. Только новая
полная checked evidence допускает один DRAFT; согласование отдельно. Иные
ошибки схемы, отрицательный verdict или непустые issues восстановлению не подлежат.

    .venv/bin/python scripts/hourly_article_test.py recover-review-protocol RECORD_ID data/article.local.json data/candidate.local.json --trace-sha256 EXACT_TRACE_SHA256 --reason "Restore only the verified advisory-count envelope; preserve raw verdict and recheck hard gates and LanguageTool"

После восстановления status должен показать один automatic_ready того же record,
без approval/dispatch. Не повторять неизвестный исход команды. Для отправки
нужны обычный policy publish с актуальным reviewed-hash и будущая дата.

Почасовой выбор/подготовку запускает Codex на компьютере владельца: он должен
работать. Уже согласованную запись исполняет worker VM без открытой панели.
Hy и Ministral делят память узла 10.20.30.143; idle-sleep и memory guard уменьшают
конкуренцию, но не блокируют чужую нагрузку. Ошибка инфраструктуры останавливает
цепочку. Для сети см. [восстановление](NETWORK_RECOVERY.md).

Полная пауза: выключить heartbeat и политику, поставить все адресаты paused,
вернуть --no-worker и исключить все 36 очередей Postiz. Панели, данные и историю
сохранить. Перед изменением runtime проверять активные отправки, сохранять backup,
обновлять CHANGELOG и версии, выполнять проверки и сохранять исходники на GitHub.

История и фактические результаты: [CHANGELOG](../CHANGELOG.md).
Актуальное поручение: [промпт](../prompts/HOURLY_ARTICLE_TESTS.md).

## Исправление уже созданного автоматического черновика

Одобрение модели не доказывает смысловую точность. Если после staging обнаружена
реальная ошибка, прежнюю версию не отправляют. `correct-staged` принимает изменённые
русские title/body без служебного footer, exact hash прежней версии и конкретную
причину расхождения с английским оригиналом:

    .venv/bin/python scripts/hourly_article_test.py correct-staged RECORD_ID data/candidate.local.json --reviewed-hash OLD_HASH --reason "Describe the verified factual correction against the English source"

Разрешён только тот же automatic_ready Postiz DRAFT, текущий preparation slot,
актуальные source/policy/hash и отсутствие любых прежних approvals/dispatches.
Все адресаты должны быть paused, активных отправок и mutation leases нет.
Старые EN/RU, metadata, raw/evidence и trace сохраняются приватно вместе с причиной.
Durable claim блокирует повтор операции и прежнюю версию до обращения к моделям.
Свежие Ministral, hard gates и LanguageTool проверяют изменённый кандидат.
Только после полного успеха один native POST обновляет тот же Postiz ID как DRAFT,
без scheduling/approval. Его прежняя дата и integration сохраняются; native Postiz
может сменить group, поэтому новая группа аудируется и импортируется.
После exact remote сверки импортируется новая ревизия, evidence и prepared hash.
Ожидаемый remote hash передаётся native guard Postiz; Hub сначала получает одну
штатную lease и только затем делает свежий GET и сравнение. Поэтому изменение
поста между предварительным чтением и записью блокируется до controller.
CAS ack обязателен. Образ Postiz 0.5.1 содержит этот guard; при обновлении
используются pinned source/compiled хеши 0.5.0 и локальная сборка Dockerfile.cas.

Неизвестный HTTP-исход, утраченный claim, конфликт версии, отказ или сбой оставляют
needs_attention/correction_blocked. Не повторять POST или модели, не сбрасывать
claim и не превращать этот путь в пересэмплирование неизменённого текста. Это не
human recheck: cookie, роль editor и человеческие checkbox не используются.
Успех исправления ещё не означает отправку: обычный policy publish отдельно
проверяет новый hash, будущую дату и интервал не менее 3600 секунд.

Ложное unknown-word для «многомасштабного анализа» разрешается только точным
source-bound правилом при наличии английского Multi-scale Ultra-high Resolution
и MUR. Написание подтверждает [первичная научная публикация](https://www.mathnet.ru/php/archive.phtml?jrnid=vmp&option_lang=rus&paperid=623&wshow=paper).
Другие слова, позиции, правила LT и реальные грамматические ошибки блокируют.
Если штатная коррекция завершилась только таким замечанием до любой записи
Postiz, допустим отдельный recover-staged-spelling с unchanged candidate,
текущим pending snapshot hash и SHA256 полного failed trace. Он сохраняет
первоначальный claim/отказ, делает отдельный durable recovery claim и повторяет
hard gates и LanguageTool. Positive Ministral берётся из точного старого trace,
с новыми моделями связи нет. Negative, иной отказ, неизвестный исход или
любая попытка remote mutation этим путём не восстанавливаются.

    .venv/bin/python scripts/hourly_article_test.py recover-staged-spelling RECORD_ID data/candidate.local.json --reviewed-hash CURRENT_PENDING_HASH --trace-sha256 FAILED_TRACE_SHA256 --reason "Recheck only the verified source-bound scientific spelling false positive; preserve the original refusal and raw review"

С версии 0.6.23 отдельно квалифицируется точное «Ниньо» в названии RONI:
«относительного океанического индекса Ниньо (RONI)» в заголовке или
«относительный океанический индекс Ниньо (RONI)» в тексте. Источник должен
содержать точное Relative Oceanic Niño Index (RONI). Написание подтверждает
[ВМО, раздел «Измерения»](https://public.wmo.int/ru/temy/yavleniya-el-nino-i-la-ninya).
Разрешено только исходное MORFOLOGIK_RULE_RU_RU/misspelling/UnknownWord на
точной UTF-16 позиции слова; другие слова, контексты и грамматика блокируют.

Для уже завершённого LT-only отказа коррекции NASA SVS 5685 предусмотрен
отдельный recover-staged-roni-spelling. Он требует ровно двух таких замечаний:
одно в заголовке и одно в тексте, полного неизменённого failed trace, strict
positive raw Ministral с пустыми issues, точных input/source/prompt/policy,
неизменённого кандидата и текущего pending snapshot hash. Старый MUR-путь
по-прежнему принимает только свой единственный термин. RONI recovery имеет
собственные durable claim, audit и history; старый failed claim и raw остаются.
До любого native CAS повторяются hard gates и свежий LT без нового Ministral.
Успех обновляет тот же DRAFT без назначения даты, approval или dispatch. Unknown, начатая
remote mutation, прежние approvals/dispatches, active sends/leases, изменение
источника/кандидата/промпта или утраченный claim не разрешают повтор. Затем
требуются обычная сверка EN/RU и отдельный publish точного нового hash.

    .venv/bin/python scripts/hourly_article_test.py recover-staged-roni-spelling RECORD_ID data/candidate.local.json --reviewed-hash CURRENT_PENDING_HASH --trace-sha256 FAILED_TRACE_SHA256 --reason "Resolve only the verified RONI proper-name dictionary false positive; preserve the exact saved positive review and failed claim"

Живая проверка 21 октября 2026, 03:43 МСК: 0.6.23 развёрнута на VM, 185
адресных тестов и 28/28 эксплуатационных checks прошли. Для NASA SVS 5685
один recovery сохранил failed correction/raw и два warnings, без нового Ministral
выполнил hard gates и свежий LT, затем native CAS того же DRAFT. Exact EN/RU,
новый hash и evidence сверены. Это automatic_ready без approvals/dispatches;
все адресаты paused. Будущие 04:06 прошли preflight с интервалом 3647,24 секунды
после фактической NISAR. Назначение и доставку это доказательство не подтверждает.

Проверка развёртывания 20 октября 2026, 00:25 МСК: 0.6.18 на рабочей VM,
632 теста и 28/28 health checks прошли. После известного отказа интервала
адресно восстановлено одно согласование того же SSHA QUEUE на прежние 00:49 МСК.
Повторных scheduling POST и оценки моделей не было; pending не означает доставку.
Heartbeat ACTIVE с этим промптом. Следующий запуск сначала сверяет это задание
и фактический MAX; до выяснения исхода новую цепочку не начинает.

Живой полный цикл 20 октября 2026: новая NASA SVS 5670 прошла Hy, независимый
Ministral, исправление реальных ошибок и повторную проверку изменённого текста.
Typed positive raw с четырьмя warnings сохранён; его advisory-протокол восстановлен
адресно без нового опроса модели, с новыми hard gates и LanguageTool. Один DRAFT
назначен на 01:50, доставлен в 01:50:14 МСК: verified dispatch, PUBLISHED и exact
MAX подтверждены. Одна попытка, интервал после SSHA 3660,42 секунды, все targets
снова paused. VM 0.6.19: 730 тестов и 28/28 health checks; heartbeat ACTIVE.

21 октября 2026: PACE опубликована в 00:02:09 МСК и точно проверена в MAX.
Следующая NASA SVS 5600 прошла содержательное исправление того же DRAFT,
сохранённый positive Ministral и свежую source-bound LT-проверку без resampling.
Живой native CAS и exact Postiz body подтверждены, исходные отказы сохранены.
Стадия automatic_ready ещё не является доставкой. Версия VM 0.6.22: 865 tests,
28/28 health checks, heartbeat ACTIVE. Планирование 01:03 отдельно подтверждается
policy approval/dispatch, а успех отправки — verified/PUBLISHED и фактическим MAX.

21 октября 2026, 07:34 МСК: исходный commit 7a6c173 версии 0.6.24 развёрнут
на VM, 351 файл сверён, 164 адресных теста и 28/28 health checks прошли.
Один прежний dispatch-only worker, heartbeat ACTIVE с точным сохранённым промптом.
Новая загрузка NASA SVS 5366 сохранила все слова и DOI, совпав с независимым
DOM-эталоном. Её подготовка остановлена STALE_SOURCE до claim/моделей: источник
2024 года не входит в действующие 8760 часов; ни дата, ни политика не изменены.
Ledger остался из 39 записей. Brunei доставлена в 07:09:11 МСК: verified,
PUBLISHED и полный MAX сверены, все пять адресатов снова paused. Загрузка
ссылок не является подтверждением перевода или доставки следующей статьи.
