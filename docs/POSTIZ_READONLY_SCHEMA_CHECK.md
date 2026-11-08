# Prisma engines при проверке и обновлении Postiz

## Обязательная проверка образа

`deployment/linux/postiz-release.sh --check` и `--up` после прежней проверки
accepted image/source, до Compose recreate, обязательно вызывают
`check-postiz-schema.py --inspect-image`. Это проверка существующих файлов
образа. Она не подключается к БД, не запускает приложение или Prisma и не
проверяет равенство схем. Принятую native-проверку, backup и восстановление
отдельной копии она не заменяет.

Проверка использует `docker --context default` через имеющийся `PATH`, включая
операторский Docker wrapper. Прямой доступ Python к Docker socket, добавление
пользователя в Docker group и изменение прав socket не нужны. Разрешён только
default context с локальным Unix endpoint; ambient `DOCKER_HOST`,
`DOCKER_CONTEXT`, `BUILDKIT_HOST` блокируются. Linux/Python 3.12 и доступный
оператору Docker CLI обязательны.

Для переданного immutable `sha256:…` создаётся один собственный контейнер с
уникальным именем и labels, `--network none`, `--read-only` и entrypoint
`/bin/true`. Контейнер **не запускается**. Все чтения `docker cp … -` относятся
к этому же контейнеру; архивы разбираются в памяти потоком, без распаковки на
host. Проверка удаляет только свой контейнер, пока он остаётся never-started.
Никакие production containers, volumes, cache или daemon settings не меняются.

Проверяемый контракт сейчас ограничен Prisma CLI и `@prisma/engines` 6.5.0,
library query engine, Linux amd64/arm64 и следующими ОС **внутри образа**:

| Image OS | VERSION_ID | OpenSSL / binary target |
| --- | --- | --- |
| Debian | 12 | 3.0.x |
| Ubuntu | 22.04, 24.04 | 3.0.x |

Для amd64 требуется `debian-openssl-3.0.x`, для arm64 —
`linux-arm64-openssl-3.0.x`. Другая версия ОС, ABI, архитектура, Prisma или
формат CLI требуют отдельного проверенного расширения контракта. Совместимость
не угадывается по самому новому cache, `ID_LIKE` или ОС сервера. Единственный
валидный по SHA engine для OpenSSL 1.1 на Debian 12 тоже блокируется.

Engine commit извлекается из зависимости `@prisma/engines-version` пакета
`@prisma/engines` того же образа. Выбирается ровно одна пара `libquery-engine`
и `schema-engine` из cache этого commit и совместимого target. Проверяются
фактические bytes/SHA256 обоих файлов и их cache `.sha256`, executable mode
schema engine, отсутствие дублей, ссылок, traversal и превышений размеров.
Эти внутренние SHA проверяют целостность содержимого accepted образа; они не
заменяют независимый SHA самого образа и native acceptance.

Приватные `claim.json`, `report.json` и `engine.env` сохраняются в новом
игнорируемом `data/deployment-schema-checks/<UUID>/` с file mode 0600 и
каталогами 0700. Claim записывается и синхронизируется до container create.
Таймаут, неизвестный результат create/remove или изменившийся контейнер
дают ненулевой результат. Существующий labelled inspection блокирует новый
create до read-only сверки оператором. Автоматических повторов и `rm -f` нет.
Стандартный вывод содержит только состояние и пути/SHA доказательств.

Отдельный запуск той же проверки:

```sh
python3 deployment/linux/check-postiz-schema.py \
  --inspect-image 'sha256:<accepted-immutable-image-id>'
```

## Read-only schema diff на восстановленной копии

Причина наблюдавшегося `EROFS`: Prisma 6.5.0 при обычном поиске engines может
попытаться обновить `utime` готового cache-файла, даже если скачивание не нужно.
У этой версии две явные переменные существующих файлов обходят cache-ветку:
`PRISMA_QUERY_ENGINE_LIBRARY` и `PRISMA_SCHEMA_ENGINE_BINARY`. Нужны **обе**.
Проверенный CLI contract и `engine.env` фиксируют эти пути без hardcoded commit
или SHA конкретной VM. Никаких download, checksum-ignore или writable rootfs
для исправления не требуется.

Публичный metadata CLI сам не запускает schema diff и не принимает URL БД.
Оператор отдельной проверки использует `engine.env` только для того же exact
image ID в собственном read-only контейнере восстановленной копии, добавляя
`--env-file <private-engine.env>` к уже проверенному запуску. DSN передаётся
отдельным приватным файлом; это исключительно clone DB с ролью без DDL/DML,
не production. Перед запуском обязательны проверенный backup/restore,
владение copy containers/PG, изоляция сети, exact schema path и исходный DB
frame; после — exit code, неизменность полного DB frame и cleanup.
`engine_environment(report)` возвращает обе переменные только для завершённой
metadata-проверки с подтверждённым cleanup.

Инфраструктурная ошибка engine/cache/timeout не равна содержательному schema
diff. Не повторяйте неизвестный исход и не запускайте `db push`, migration или
приложение, чтобы получить зелёную проверку. Эта tooling-правка отдельно от
ранее принятого image/source 0.14.0; она не изменяет bytes принятого runtime.

## Границы copy harness

Если собственный контейнер ограничен Docker `local` logging с `max-file=1`,
явно задавайте `compress=false`: унаследованное `compress=true` несовместимо
с одной ротацией. Менять daemon settings ради этого не требуется. Metadata
collector использует совместимые опции, хотя его контейнер не запускается.

После остановки copy API ошибка обычного `socket.bind` может означать TCP
`TIME_WAIT`, а не живой listener. Сверяйте фактический `LISTEN` для IPv4/IPv6
либо используйте корректный `SO_REUSEADDR` probe; не делайте слепой reset или
повторный start. В metadata collector проверки свободного TCP-порта нет.

Локальные синтетические tests проверяют разбор, SHA, выбор ABI, обязательность
preflight и failure/cleanup. Они не подтверждают выполнение Docker/Prisma на
другом Linux сервере, успешный schema diff или живую публикацию.
