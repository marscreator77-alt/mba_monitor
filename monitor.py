#!/usr/bin/env python3
"""
Мониторинг доступности и работоспособности VPS и сайтов на них,
с уведомлениями в Telegram.

Режимы:
  python monitor.py check   — проверить все VPS, мгновенно уведомить при
                               падении/восстановлении, сохранить состояние.
  python monitor.py hourly  — отправить сводку по текущему состоянию
                               (последний известный результат check).
  python monitor.py test    — отправить тестовое сообщение в Telegram.
"""
import argparse
import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
STATE_PATH = BASE_DIR / "state.json"

RETRIES = 3          # попыток TCP-проверки перед тем как признать хост упавшим
RETRY_DELAY = 8       # секунд между попытками
REALERT_INTERVAL = 1800  # повторно напоминать о всё ещё лежащем хосте раз в 30 мин


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=15,
    )
    resp.raise_for_status()


def tcp_check(host: str, port: int, timeout: int = 5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except OSError as e:
        return False, str(e)


def ssh_service_check(host, ssh_user, ssh_port, key_path, services) -> dict:
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host,
        port=ssh_port,
        username=ssh_user,
        key_filename=key_path,
        timeout=10,
        look_for_keys=False,
        allow_agent=False,
    )
    results = {}
    try:
        for svc in services:
            if svc.startswith("proc:"):
                pattern = svc[len("proc:"):]
                cmd = f"pgrep -f {pattern!r} >/dev/null && echo active || echo inactive"
                expected = "active"
            elif svc.startswith("docker:"):
                container = svc[len("docker:"):]
                cmd = f"sudo /usr/local/bin/docker-container-check.sh {container}"
                expected = "true"
            else:
                cmd = f"systemctl is-active {svc}"
                expected = "active"
            _, stdout, _ = client.exec_command(cmd, timeout=10)
            out = stdout.read().decode().strip()
            results[svc] = out == expected
    finally:
        client.close()
    return results


def check_host(vps: dict, ssh_key_path):
    ok, last_reason = False, None
    for attempt in range(RETRIES):
        ok, last_reason = tcp_check(vps["host"], vps.get("port", 22))
        if ok:
            break
        if attempt < RETRIES - 1:
            time.sleep(RETRY_DELAY)

    if not ok:
        return False, f"недоступен по TCP {vps.get('port', 22)}: {last_reason}"

    services = vps.get("services") or []
    if services:
        if not ssh_key_path:
            return False, "заданы services, но SSH_PRIVATE_KEY не настроен"
        try:
            results = ssh_service_check(
                vps["host"],
                vps.get("ssh_user", "root"),
                vps.get("ssh_port", 22),
                ssh_key_path,
                services,
            )
        except Exception as e:
            return False, f"SSH/проверка сервисов не удалась: {e}"
        failed = [s for s, up in results.items() if not up]
        if failed:
            return False, f"не запущены: {', '.join(failed)}"

    return True, None


def check_website(site: dict):
    url = site["url"]
    last_reason = None
    for attempt in range(RETRIES):
        try:
            resp = requests.get(url, timeout=10, allow_redirects=True)
            if resp.status_code < 500:
                return True, None
            last_reason = f"HTTP {resp.status_code}"
        except requests.RequestException as e:
            last_reason = str(e)
        if attempt < RETRIES - 1:
            time.sleep(RETRY_DELAY)
    return False, last_reason


def run_checks(config: dict, ssh_key_path) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    results = {}
    for vps in config.get("vps", []):
        ok, reason = check_host(vps, ssh_key_path)
        results[vps["name"]] = {"ok": ok, "reason": reason, "checked_at": now}
    for site in config.get("websites", []):
        ok, reason = check_website(site)
        results[site["name"]] = {"ok": ok, "reason": reason, "checked_at": now}
    return results


def cmd_check(config, token, chat_id, ssh_key_path) -> None:
    old_state = load_state()
    fresh = run_checks(config, ssh_key_path)
    now = time.time()
    new_state = {}

    for name, res in fresh.items():
        prev = old_state.get(name, {"ok": True})
        was_ok = prev.get("ok", True)

        if res["ok"] and not was_ok:
            send_telegram(token, chat_id, f"✅ <b>{name}</b> снова в порядке.")
        elif not res["ok"] and was_ok:
            send_telegram(
                token, chat_id,
                f"🔴 <b>{name}</b> недоступен!\nПричина: {res['reason']}",
            )
            res["last_alert"] = now
        elif not res["ok"] and not was_ok:
            last_alert = prev.get("last_alert", 0)
            if now - last_alert > REALERT_INTERVAL:
                send_telegram(
                    token, chat_id,
                    f"🔴 <b>{name}</b> всё ещё недоступен.\nПричина: {res['reason']}",
                )
                res["last_alert"] = now
            else:
                res["last_alert"] = last_alert

        new_state[name] = res

    # записи VPS, которых больше нет в config.yaml (например, после смены
    # имени/IP), сюда не попадают — состояние всегда отражает только
    # актуальный список хостов
    save_state(new_state)


def format_summary(state: dict, title_ok: str, title_bad: str) -> str:
    lines = []
    all_ok = True
    for name, res in state.items():
        if res.get("ok"):
            lines.append(f"✅ {name}")
        else:
            all_ok = False
            lines.append(f"🔴 {name} — {res.get('reason')}")

    header = title_ok if all_ok else title_bad
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"{header} ({ts})\n\n" + "\n".join(lines)


def cmd_hourly(config, token, chat_id, ssh_key_path) -> None:
    state = load_state()
    if not state:
        state = run_checks(config, ssh_key_path)
        save_state(state)

    msg = format_summary(state, "✅ Все VPS в порядке", "⚠️ Есть проблемы")
    send_telegram(token, chat_id, msg)


def cmd_status(config, token, chat_id, ssh_key_path) -> None:
    """Свежая проверка по запросу (/status) — всегда шлёт сводку, даже без изменений."""
    cmd_check(config, token, chat_id, ssh_key_path)
    state = load_state()
    msg = format_summary(state, "🔍 Статус: всё ок", "🔍 Статус: есть проблемы")
    send_telegram(token, chat_id, msg)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["check", "hourly", "status", "test"])
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        sys.exit("Нужны переменные окружения TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")

    if args.mode == "test":
        send_telegram(token, chat_id, "🔧 Тестовое сообщение от vps-monitor. Всё настроено верно.")
        return

    ssh_key_path = None
    ssh_key_content = os.environ.get("SSH_PRIVATE_KEY")
    if ssh_key_content:
        ssh_key_path = "/tmp/vps_monitor_key"
        Path(ssh_key_path).write_text(ssh_key_content, encoding="utf-8")
        os.chmod(ssh_key_path, 0o600)

    config = load_config()

    if args.mode == "check":
        cmd_check(config, token, chat_id, ssh_key_path)
    elif args.mode == "status":
        cmd_status(config, token, chat_id, ssh_key_path)
    else:
        cmd_hourly(config, token, chat_id, ssh_key_path)


if __name__ == "__main__":
    main()
