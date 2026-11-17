"""Бот подписки с оплатой через штатные платные ссылки Telegram."""
import json
import os
from pathlib import Path
import re
import sqlite3
import socket
import ssl
import time
from urllib.request import Request, urlopen, getproxies
from urllib.error import HTTPError, URLError

class TelegramError(Exception):
    def __init__(self, code):
        self.code = code

def api(method, **fields):
    request = Request(
        f"https://api.telegram.org/bot{os.environ['BOT_TOKEN']}/{method}",
        data=json.dumps(fields).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=40) as response:
            result = json.load(response)
    except HTTPError as error:
        raise TelegramError(error.code) from None
    if not result.get("ok"):
        raise TelegramError(result.get("error_code", 500))
    return result["result"]

def screen(db, user, owner, channel, price):
    if channel is None:
        return ("Настройка канала\n\nВладелец должен переслать сюда публикацию из своего канала.", [])
    return (
        f"Клуб практики\n\nМатериалы закрытого канала и доступ к сообществу.\n"
        f"Подписка: {price} ⭐ за 30 дней.\n\n"
        "Оплату и автоматическое продление оформляет Telegram. "
        "Перед подтверждением проверьте условия на экране оплаты.",
        [[{"text": "Посмотреть подписку", "callback_data": "buy"}],
         [{"text": "Как отменить продление", "callback_data": "help"}]],
    )

def send(chat, text, rows=None):
    return api("sendMessage", chat_id=chat, text=text,
               reply_markup={"inline_keyboard": rows or []})

def handle(db, update, owner, price):
    message = update.get("message")
    callback = update.get("callback_query")
    channel_row = db.execute("SELECT value FROM settings WHERE key='channel'").fetchone()
    channel = int(channel_row[0]) if channel_row else None
    if message and message["chat"]["type"] == "private":
        chat = message["chat"]["id"]
        origin = message.get("forward_origin", {})
        if chat == owner and origin.get("type") == "channel":
            candidate = origin["chat"]["id"]
            bot = api("getMe")
            rights = api("getChatMember", chat_id=candidate, user_id=bot["id"])
            if rights.get("status") != "administrator" or not rights.get("can_invite_users"):
                send(chat, "Добавьте бота администратором канала с правом приглашать пользователей.")
                return
            info = api("getChat", chat_id=candidate)
            if info["type"] != "channel":
                send(chat, "Нужен канал Telegram.")
                return
            if channel is not None and candidate != channel:
                send(chat, "Канал уже настроен. Для смены используйте отдельный проект бота.")
                return
            with db:
                db.execute("INSERT OR REPLACE INTO settings VALUES ('channel', ?)", (str(candidate),))
            send(chat, "Канал подключён. Отправьте /start для просмотра тарифа.")
            return
        text = message.get("text", "").split(maxsplit=1)
        command = text[0].split("@")[0] if text else ""
        if command == "/id":
            send(chat, f"Ваш Telegram ID: {chat}")
        elif command in ("/help", "/paysupport"):
            send(chat, "Отмена продления доступна в настройках Telegram Stars → Подписки. "
                 "По вопросам доступа обратитесь к владельцу канала.")
        elif command == "/admin":
            send(chat, f"Канал: {channel or 'не подключён'}\nТариф: {price} ⭐ / 30 дней\n"
                 "Платежи и подписки доступны в настройках канала."
                 if chat == owner else "Раздел доступен владельцу.")
        else:
            text, rows = screen(db, chat, owner, channel, price)
            send(chat, text, rows)
        return
    if not callback or not callback.get("message"):
        return
    api("answerCallbackQuery", callback_query_id=callback["id"])
    chat = callback["message"]["chat"]
    if chat["type"] != "private":
        return
    if callback.get("data") == "help":
        send(chat["id"], "Откройте Telegram → Настройки → Мои звёзды → Подписки. "
             "Выберите канал и отключите продление. Доступ сохранится до окончания оплаченного периода.")
    elif callback.get("data") == "buy" and channel:
        row = db.execute("SELECT value FROM settings WHERE key=?", (f"link:{price}",)).fetchone()
        if row:
            link = row[0]
        else:
            result = api("createChatSubscriptionInviteLink", chat_id=channel,
                         name=f"Клуб · {price} Stars", subscription_period=2592000,
                         subscription_price=price)
            link = result["invite_link"]
            with db:
                db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (f"link:{price}", link))
        send(chat["id"], f"Доступ на 30 дней — {price} ⭐\n\n"
             "По кнопке откроется оформление подписки в Telegram.",
             [[{"text": "Открыть оплату в Telegram", "url": link}]])

def network_reason(error):
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "Не удалось проверить SSL-сертификат Telegram. Проверьте сертификаты и время на хостинге."
    if isinstance(reason, socket.gaierror):
        return "DNS хостинга не смог определить адрес api.telegram.org."
    if isinstance(reason, TimeoutError):
        return "Истекло время ожидания api.telegram.org."
    if isinstance(reason, OSError):
        return f"Ошибка сети хостинга: {type(reason).__name__}, код {reason.errno}."
    return f"Ошибка подключения: {type(reason).__name__}."


def check_network():
    proxies = getproxies()
    print("Прокси HTTPS настроен: " + ("да" if proxies.get("https") else "нет"), flush=True)
    for family, label in ((socket.AF_INET, "IPv4"), (socket.AF_INET6, "IPv6")):
        try:
            addresses = socket.getaddrinfo("api.telegram.org", 443, family, socket.SOCK_STREAM)
            address = addresses[0][4]
            with socket.socket(family, socket.SOCK_STREAM) as connection:
                connection.settimeout(5)
                connection.connect(address)
                with ssl.create_default_context().wrap_socket(
                    connection, server_hostname="api.telegram.org"
                ):
                    print(f"{label}: прямое соединение и SSL работают.", flush=True)
        except (OSError, URLError) as error:
            print(f"{label}: " + network_reason(error), flush=True)


def main():
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", os.environ.get("BOT_TOKEN", "")):
        raise SystemExit("Задайте BOT_TOKEN в секретных переменных Bothost.")
    owner_text = os.environ.get("OWNER_CHAT_ID", "").strip()
    owner = int(owner_text) if owner_text.isascii() and owner_text.isdigit() else 0
    price = int(os.environ.get("PRICE_STARS", "100"))
    if not 1 <= price <= 10000:
        raise SystemExit("PRICE_STARS должно быть от 1 до 10000.")
    folder = Path(os.environ.get("DATA_DIR", "data"))
    folder.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(folder / "subscriptions.sqlite3") as db:
        db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        offset = None
        connected = False
        diagnosed = False
        print("Проверяю подключение к Telegram…", flush=True)
        while True:
            try:
                if not connected:
                    account = api("getMe")
                    print(f"Подключён @{account['username']}. Отправьте /id этому боту.", flush=True)
                    connected = True
                updates = api("getUpdates", offset=offset, timeout=25,
                              allowed_updates=["message", "callback_query"])
                for update in updates:
                    try:
                        handle(db, update, owner, price)
                    except TelegramError as error:
                        if error.code not in (400, 403):
                            raise
                        print(f"Telegram отклонил действие: {error.code}", flush=True)
                    offset = update["update_id"] + 1
            except TelegramError as error:
                if error.code in (401, 404, 409):
                    raise SystemExit(f"Проверьте токен или другой запуск бота: {error.code}")
                time.sleep(5)
            except (URLError, TimeoutError, ConnectionError) as error:
                connected = False
                print(network_reason(error) + " Повтор через 5 секунд.", flush=True)
                if not diagnosed:
                    check_network()
                    diagnosed = True
                time.sleep(5)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Бот остановлен.")
