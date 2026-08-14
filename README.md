# vps-monitor

Мониторинг доступности и работоспособности VPS 24/7 с уведомлениями в Telegram.

- Проверка каждые 5 минут: TCP-доступность хоста + (опционально) состояние
  конкретных systemd-сервисов/процессов по SSH.
- При падении — мгновенное уведомление. При восстановлении — тоже.
- Раз в час — сводка "всё ок" (или список проблем, если что-то не так).
- Работает не на самих VPS, а во внешнем GitHub Actions — если ваш VPS
  ляжет полностью, мониторинг это не затронет.

## 1. Создать Telegram-бота

1. Напишите [@BotFather](https://t.me/BotFather) → `/newbot`, получите `TELEGRAM_BOT_TOKEN`.
2. Напишите своему новому боту любое сообщение (например "привет"), иначе он
   не сможет писать вам первым.
3. Узнайте свой `chat_id`: откройте в браузере
   `https://api.telegram.org/bot<TOKEN>/getUpdates` (подставив токен) и
   найдите `"chat":{"id": ...}` в ответе. Проще — напишите
   [@userinfobot](https://t.me/userinfobot), он сразу пришлёт ваш ID.

## 2. Настроить SSH-доступ (только если нужны проверки сервисов)

Если вам достаточно знать, что VPS "жив" по сети — SSH не нужен, просто не
указывайте `services` в `config.yaml`.

Если нужно проверять конкретные сервисы:

1. На каждом VPS создайте отдельного непривилегированного пользователя для
   мониторинга (не root):
   ```bash
   sudo adduser --disabled-password monitor
   sudo usermod -aG systemd-journal monitor   # если нужно читать статус юнитов
   ```
2. Сгенерируйте отдельную SSH-пару **только для этого** (на своей машине):
   ```bash
   ssh-keygen -t ed25519 -f ./vps_monitor_key -N ""
   ```
3. Добавьте публичный ключ (`vps_monitor_key.pub`) в
   `/home/monitor/.ssh/authorized_keys` на каждом VPS.
4. Приватный ключ (`vps_monitor_key`) целиком пойдёт в GitHub Secret
   `SSH_PRIVATE_KEY` (см. ниже). Никому больше не показывайте этот файл.

### Проверка docker-контейнеров (`docker:container-name`)

Добавлять `monitor` в группу `docker` **не нужно и не рекомендуется** — это
даёт фактически root-доступ на хосте через docker-сокет. Вместо этого на
VPS, где нужно проверять контейнеры, разово настройте узкий sudo-доступ
только к чтению статуса конкретных контейнеров:

```bash
cat <<'SCRIPT' > /usr/local/bin/docker-container-check.sh
#!/bin/bash
exec /usr/bin/docker inspect -f '{{.State.Running}}' "$1"
SCRIPT
chmod 755 /usr/local/bin/docker-container-check.sh

cat <<'SUDOERS' > /etc/sudoers.d/monitor-docker
monitor ALL=(root) NOPASSWD: /usr/local/bin/docker-container-check.sh container-1, /usr/local/bin/docker-container-check.sh container-2
SUDOERS
chmod 440 /etc/sudoers.d/monitor-docker
visudo -c
```

Перечислите в строке `SUDOERS` через запятую ровно те имена контейнеров,
которые хотите мониторить (`docker ps --format '{{.Names}}'` покажет
актуальный список) — `monitor` сможет выполнить `sudo` только с этими
точными аргументами и ничего больше. Затем в `config.yaml` укажите
`"docker:имя-контейнера"` в `services` для этой VPS.

## 3. Разместить репозиторий на GitHub

Создайте **приватный** репозиторий и запушьте эту папку:

```bash
cd vps-monitor
git init
git add .
git commit -m "vps monitor"
git branch -M main
git remote add origin <URL вашего приватного репозитория>
git push -u origin main
```

## 4. Добавить секреты репозитория

В репозитории: Settings → Secrets and variables → Actions → New repository secret:

| Имя | Значение |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен от BotFather |
| `TELEGRAM_CHAT_ID` | ваш chat_id |
| `SSH_PRIVATE_KEY` | содержимое приватного ключа `vps_monitor_key` (только если нужны проверки сервисов) |

## 5. Заполнить config.yaml

Отредактируйте [config.yaml](config.yaml) — впишите реальные IP/хосты и
(опционально) список сервисов для каждого VPS. Закоммитьте и запушьте.

## 6. Проверить

Actions → выберите workflow "VPS check" или "VPS hourly summary" →
**Run workflow** (ручной запуск), убедитесь, что сообщение пришло в Telegram.

Можно также прогнать локально перед пушем:

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=xxx
export TELEGRAM_CHAT_ID=xxx
python monitor.py test     # проверка, что бот вообще может писать вам
python monitor.py check
python monitor.py hourly
```

## 7. Команда /status в Telegram (мгновенная проверка по запросу)

Обычный `check.yml`/`hourly.yml` работают по расписанию GitHub `schedule`,
которое (см. раздел "Важные ограничения" ниже) может задерживаться на
часы. Чтобы `/status` в Telegram отвечал быстро, нужен отдельный
webhook-приёмник — маленький бесплатный Cloudflare Worker, который Telegram
дёргает мгновенно в момент отправки сообщения, а он тут же запускает
GitHub-воркфлоу `status.yml` через API (`workflow_dispatch` стартует за
секунды, в отличие от `schedule`).

Код воркера — [cloudflare-worker/status-webhook.js](cloudflare-worker/status-webhook.js).

### 7.1. Создать Cloudflare Worker

1. Зарегистрируйтесь на [dash.cloudflare.com](https://dash.cloudflare.com)
   (бесплатно, без карты), если аккаунта ещё нет.
2. В меню слева: **Workers & Pages → Create → Create Worker**. Дайте имя,
   например `vps-monitor-status`, нажмите **Deploy** (заготовка "Hello
   World" — это нормально, дальше заменим код).
3. Откройте воркер → **Edit code** — вставьте туда содержимое файла
   [cloudflare-worker/status-webhook.js](cloudflare-worker/status-webhook.js)
   целиком, заменив всё, что было. **Save and Deploy**.
4. Скопируйте URL воркера — он вида
   `https://vps-monitor-status.<ваш-поддомен>.workers.dev`.

### 7.2. Добавить секреты воркера

В воркере: **Settings → Variables and Secrets → Add** — добавьте по одному,
каждый раз выбирая тип **Secret** (шифруется):

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | тот же токен бота, что и в GitHub Secrets |
| `TELEGRAM_CHAT_ID` | `718792023` |
| `WEBHOOK_SECRET` | случайная строка (см. ниже) |
| `GITHUB_TOKEN` | fine-grained PAT, см. пункт 7.3 |
| `GITHUB_OWNER` | `marscreator77-alt` |
| `GITHUB_REPO` | `mba_monitor` |

Значение для `WEBHOOK_SECRET` (сгенерировано случайно, используйте как есть):

```
95a31181144a02869b05efdb64fdf36b299d1554d78b08a0f4a446ace1571fd7
```

### 7.3. Создать GitHub-токен для запуска workflow

1. [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new)
2. **Resource owner**: `marscreator77-alt`.
3. **Repository access**: Only select repositories → `mba_monitor`.
4. **Permissions → Repository permissions → Actions**: `Read and write`.
5. Generate token, скопируйте значение (показывается один раз) — это и
   есть `GITHUB_TOKEN` из пункта 7.2. Больше нигде, кроме секрета в
   Cloudflare Worker, его вставлять не нужно.

### 7.4. Подключить Telegram webhook

Выполните у себя в терминале (переменные `TELEGRAM_BOT_TOKEN` уже должны
быть экспортированы из более ранних шагов; результат не пересылайте в чат
целиком — там мелькает токен):

```bash
curl -F "url=https://vps-monitor-status.<ваш-поддомен>.workers.dev/" \
     -F "secret_token=95a31181144a02869b05efdb64fdf36b299d1554d78b08a0f4a446ace1571fd7" \
     "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook"
```

Ожидаемый ответ: `{"ok":true,"result":true,"description":"Webhook was set"}`.

### 7.5. Проверить

Напишите боту `/status` в Telegram. В течение секунды должно прийти
`⏳ Запускаю проверку VPS...`, а через ~20–30 секунд — полная сводка вида
`🔍 Статус: всё ок` (или список проблем, если что-то упало).

## Как это работает

- `monitor.py check` — гоняется по расписанию раз в 15 минут (`check.yml`). Сравнивает
  текущий результат с сохранённым в `state.json`. Если статус хоста
  изменился (up→down или down→up) — шлёт уведомление сразу. Если хост уже
  давно лежит — не спамит, а напоминает раз в 30 минут.
- `monitor.py hourly` — гоняется раз в час (`hourly.yml`), просто присылает
  сводку по последнему известному состоянию.
- `monitor.py status` — запускается только вручную/по API (`status.yml`,
  без расписания), через Cloudflare Worker по команде `/status` в Telegram
  (см. раздел 7 выше). Делает свежую проверку и всегда присылает сводку,
  даже если ничего не изменилось.
- Состояние (`state.json`) коммитится обратно в репозиторий workflow'ом
  `check.yml`, чтобы переживать между запусками (GitHub Actions runner —
  одноразовый, свою файловую систему не сохраняет).

## Важные ограничения

- **`schedule` в GitHub Actions — это "best effort", а не гарантия.**
  На практике расписание `*/5 * * * *` наблюдалось запускающимся раз в
  2–4 часа вместо каждых 5 минут — GitHub сильно троттлит частые
  scheduled-запуски, особенно на бесплатных аккаунтах, вплоть до того что
  часть запусков просто пропускается без предупреждения. Интервал в этом
  репозитории увеличен до 15 минут — это немного снижает троттлинг, но
  **полной гарантии точного расписания GitHub всё равно не даёт**. Если
  задержки в часы неприемлемы — см. альтернативы ниже.
- GitHub автоматически **отключает scheduled workflows**, если в
  репозитории 60 дней не было активности (пушей/коммитов). Раз `check.yml`
  сам коммитит `state.json` при каждом изменении, при регулярных падениях
  проблем не будет, но если всё стабильно месяцами — раз в 1–2 месяца
  сделайте пустой коммит или зайдите и нажмите "Run workflow" вручную.
- Если репозиторий приватный и вы на бесплатном личном GitHub-аккаунте —
  Actions включены и бесплатны в пределах щедрого лимита минут; для этой
  задачи вы в него не упрётесь.
- **Альтернативы, если нужна предсказуемая по времени реакция:**
  - Внешний бесплатный cron-сервис (например cron-job.org) дёргает по
    точному расписанию GitHub REST API
    (`POST /repos/{owner}/{repo}/actions/workflows/check.yml/dispatches`)
    вместо встроенного `schedule` — сама проверка остаётся в GitHub
    Actions (значит и SSH/docker-логика не меняется), но *запуск*
    становится точным, а не по прихоти GitHub'овского планировщика.
  - Специализированный сервис вроде UptimeRobot/Better Stack — точное
    расписание "из коробки", но там нет SSH-проверки конкретных
    docker-контейнеров, только сетевые/HTTP-проверки.
  - Отдельный always-on хост с постоянным процессом (то, от чего вы
    изначально осознанно отказались, потому что не хотели зависеть от
    ещё одного сервера).
