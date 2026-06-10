"""
Telegram-бот: курьер присылает PDF-отчёт за день -> бот разбирает его,
сохраняет в SQLite (с дедупом по дате) и считает среднее время доставки
за текущую неделю (Пн–Вс), от которого зависит статус аккаунта.

Что изменилось относительно первой версии:
- данные в SQLite, а не в памяти (переживают перезапуск);
- читается дата отчёта -> дедуп и недельные окна;
- ∅ (нет времени доставки на смене после 23:00) корректно исключается из среднего,
  а не подменяется временем выезда.
"""

import os
from datetime import date

import telebot
from telebot import types

try:
    from dotenv import load_dotenv  # локально читаем .env; на проде переменные задаёт хостинг
    load_dotenv()
except ImportError:
    pass

import db
from parser import (
    extract_words_from_pdf,
    detect_report_kind,
    looks_like_report_filename,
    parse_words,
    seconds_to_time,
)

MAX_PDF_BYTES = 3 * 1024 * 1024   # реальные отчёты ~700 КБ; всё крупнее — отклоняем

try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or "0")  # Telegram ID владельца; /admin только ему
except ValueError:
    ADMIN_ID = 0

TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise ValueError("❌ TOKEN not found! Положи токен бота в переменную окружения TOKEN.")

bot = telebot.TeleBot(TOKEN)
db.init_db()


def _has_access(user_id: int) -> bool:
    """Доступ есть у владельца и у активных подписчиков."""
    return user_id == ADMIN_ID or db.is_subscribed(user_id)


def _no_access_msg(user_id: int) -> str:
    return (
        "🔒 Доступ к боту — по подписке.\n"
        f"Чтобы получить доступ, передай этот ID администратору: {user_id}"
    )


def _setup_commands() -> None:
    """Меню команд (синяя кнопка Menu). Админские — только в чате владельца."""
    public = [
        types.BotCommand("start", "Запуск и статус доступа"),
        types.BotCommand("stats", "Сводка по неделям"),
        types.BotCommand("reset", "Удалить мои данные"),
    ]
    try:
        bot.set_my_commands(public)
        if ADMIN_ID:
            bot.set_my_commands(
                public + [
                    types.BotCommand("admin", "Админ-сводка"),
                    types.BotCommand("grant", "Выдать подписку: /grant id [дней]"),
                    types.BotCommand("revoke", "Закрыть подписку: /revoke id"),
                ],
                scope=types.BotCommandScopeChat(ADMIN_ID),
            )
    except Exception as e:  # noqa: BLE001
        print(f"[warn] set_my_commands: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# СТАТУС/УРОВЕНЬ АККАУНТА — отложено. Это tier/очки (напр. "Silver 🥈 70"),
# зависит не только от среднего времени и различается по магазинам. Сюда же —
# будущие рекомендации курьеру (сколько ∅-доставок до нужного среднего, запас
# по времени, манипуляция доставка⇄выезд). Требует отдельной проработки.
# ─────────────────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["start"])
def cmd_start(message):
    uid = message.from_user.id
    if uid == ADMIN_ID:
        access = "✅ Доступ администратора."
    elif db.is_subscribed(uid):
        access = f"✅ Доступ активен до {db.subscription_until(uid)}."
    else:
        access = ("🔒 Доступ к боту — по подписке.\n"
                  f"Передай этот ID администратору для активации: {uid}")
    bot.send_message(
        message.chat.id,
        "👋 Привет! Пересылай сюда ежедневный PDF-отчёт — я распознаю дату и "
        "время доставки/выезда, сохраню по дням и посчитаю среднее за неделю (Пн–Вс).\n\n"
        f"{access}\n\n"
        "Команды:\n"
        "/stats — сводка по неделям\n"
        "/reset — удалить все мои данные",
        reply_markup=types.ReplyKeyboardRemove(),
    )


@bot.message_handler(commands=["reset"])
def cmd_reset(message):
    n = db.delete_user_data(message.from_user.id)
    bot.send_message(message.chat.id, f"🗑 Удалено записей: {n}. Можно присылать отчёты заново.")


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    if not _has_access(message.from_user.id):
        bot.send_message(message.chat.id, _no_access_msg(message.from_user.id))
        return
    bot.send_message(message.chat.id, _format_all_weeks(message.from_user.id))


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    # отвечаем только владельцу; остальным — молча игнорируем (команда не выдаёт себя)
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    s = db.admin_stats()
    bot.send_message(
        message.chat.id,
        "🛠 Админ-сводка\n"
        f"👥 Пользователей: {s['total_users']}\n"
        f"📄 Отчётов всего: {s['total_reports']}\n"
        f"🗓 За неделю ({s['week_start']}–{s['week_end']}): "
        f"{s['week_users']} польз. · {s['week_reports']} отч.\n"
        f"💳 Активных подписок: {s['active_subs']}\n"
        f"🕒 Последняя активность: {s['last_activity'] or '—'}",
    )


@bot.message_handler(commands=["grant"])
def cmd_grant(message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.send_message(message.chat.id, "Использование: /grant <user_id> [дней=30]")
        return
    target = int(parts[1])
    days = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 30
    until = db.grant_subscription(target, days)
    bot.send_message(message.chat.id, f"✅ Подписка для {target} активна до {until} (+{days} дн.)")
    try:
        bot.send_message(target, f"🎉 Доступ активирован до {until}. Можешь присылать отчёты!")
    except Exception:  # noqa: BLE001
        pass  # пользователь мог ещё не открыть чат с ботом


@bot.message_handler(commands=["revoke"])
def cmd_revoke(message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.send_message(message.chat.id, "Использование: /revoke <user_id>")
        return
    target = int(parts[1])
    ok = db.revoke_subscription(target)
    bot.send_message(
        message.chat.id,
        f"{'✅ Подписка закрыта' if ok else '⚠️ Подписки и не было'} для {target}",
    )


@bot.message_handler(content_types=["document"])
def handle_pdf(message):
    user_id = message.from_user.id
    doc = message.document
    fname = doc.file_name or ""

    # 0) доступ по подписке (владелец — всегда)
    if not _has_access(user_id):
        bot.send_message(message.chat.id, _no_access_msg(user_id))
        return

    # 1) фильтр по имени ДО скачивания: принимаем только отчёты вида
    #    'YYYY-MM-DD-...podsumowanie.pdf'. Чужие файлы даже не качаем и не парсим.
    if not looks_like_report_filename(fname):
        bot.send_message(
            message.chat.id,
            "🤖 Я принимаю только файлы-отчёты (PDF «…podsumowanie.pdf»). "
            "Перешли отчёт как есть, не переименовывая.",
        )
        return

    # 2) лимит размера — тоже до скачивания (защита от огромных/битых файлов)
    if doc.file_size and doc.file_size > MAX_PDF_BYTES:
        bot.send_message(
            message.chat.id,
            f"❌ Файл слишком большой (> {MAX_PDF_BYTES // (1024 * 1024)} МБ). Это точно отчёт?",
        )
        return

    try:
        file_info = bot.get_file(doc.file_id)
        data = bot.download_file(file_info.file_path)
        words = extract_words_from_pdf(data)
    except Exception as e:  # noqa: BLE001
        print(f"[error] чтение PDF не удалось (user {user_id}): {e}")  # в лог сервера, не юзеру
        bot.send_message(message.chat.id, "❌ Не удалось прочитать PDF. Это корректный отчёт?")
        return

    kind = detect_report_kind(words)
    if kind == "weekly":
        bot.send_message(
            message.chat.id,
            "📅 Это НЕДЕЛЬНЫЙ отчёт (Twoje tygodniowe podsumowanie). "
            "Я веду подсчёт по ДНЕВНЫМ отчётам — пришли, пожалуйста, отчёт за один день.",
        )
        return
    if kind != "daily":
        bot.send_message(
            message.chat.id,
            "❌ Не похоже на дневной отчёт «Twoje podsumowanie». Проверь файл.",
        )
        return

    report = parse_words(words, filename=fname)
    if not report.report_date:
        bot.send_message(
            message.chat.id,
            "❌ Не нашёл дату в отчёте. Это точно отчёт «Twoje podsumowanie»?",
        )
        return

    try:
        action = db.upsert_report(user_id, report)
    except Exception as e:  # noqa: BLE001
        print(f"[error] сохранение не удалось (user {user_id}): {e}")  # в лог сервера
        bot.send_message(message.chat.id, "❌ Не удалось сохранить отчёт, попробуй ещё раз.")
        return

    # подтверждение по дню
    when = "обновил" if action == "updated" else "принял"
    before_23 = report.orders_before_23
    after_23 = max(report.orders_all - report.orders_before_23, 0)
    orders_line = f"📦 Заказов: до 23:00 — {before_23} · после 23:00 — {after_23}"

    if report.delivery_sec is None:
        day_line = (
            f"✅ {when} отчёт за {report.report_date}.\n"
            f"⌀ Время доставки за этот день не считается (смена после 23:00).\n"
            f"🛵 Время выезда: {seconds_to_time(report.start_sec)} · {report.earnings:.2f} zł\n"
            f"{orders_line}"
        )
    else:
        day_line = (
            f"✅ {when} отчёт за {report.report_date}.\n"
            f"⏱ Время доставки: {seconds_to_time(report.delivery_sec)} · {report.earnings:.2f} zł\n"
            f"{orders_line}"
        )

    # после загрузки показываем неделю ИМЕННО присланного отчёта (а не сегодняшнюю)
    week = db.weekly_stats(user_id, ref=date.fromisoformat(report.report_date))
    bot.send_message(message.chat.id, day_line + "\n\n" + _week_block(week))


MAX_WEEKS_IN_STATS = 12  # лимит, чтобы не упереться в макс. длину сообщения Telegram (~4096)


# Бонус к каждому заказу при разных статусах (zł). Итог = заработок + заказы × бонус.
TIER_BONUS = {"gold": 1.5, "silver": 0.75, "bronze": 0.5}


def _week_block(w) -> str:
    """Одна календарная неделя в виде текстового блока."""
    o = w.total_orders
    gold = w.total_earnings + o * TIER_BONUS["gold"]
    silver = w.total_earnings + o * TIER_BONUS["silver"]
    bronze = w.total_earnings + o * TIER_BONUS["bronze"]
    return "\n".join([
        f"🗓 {date.fromisoformat(w.week_start).strftime('%d.%m')} — "
        f"{date.fromisoformat(w.week_end).strftime('%d.%m.%Y')}",
        f"⏱ Доставка: {seconds_to_time(w.avg_delivery_sec)}",
        f"🛵 Выезд: {seconds_to_time(w.avg_start_sec)}",
        f"📦 Заказов: {o}",
        f"💰 Заработок: {w.total_earnings:.2f} zł "
        f"(🥇 {gold:.2f} · 🥈 {silver:.2f} · 🥉 {bronze:.2f})",
        f"📄 Отчётов за неделю: {w.days_total}",
    ])


def _format_all_weeks(user_id: int) -> str:
    weeks = db.all_weeks_stats(user_id)
    if not weeks:
        return "📊 Отчётов пока нет. Пришли PDF-отчёт — посчитаю."
    weeks = list(reversed(weeks))            # свежие недели сверху
    shown = weeks[:MAX_WEEKS_IN_STATS]
    text = "📊 Сводка по неделям (свежие сверху)\n\n" + "\n\n".join(_week_block(w) for w in shown)
    if len(weeks) > len(shown):
        text += (f"\n\n… и ещё {len(weeks) - len(shown)} нед. ранее "
                 f"(показаны последние {MAX_WEEKS_IN_STATS}).")
    return text


if __name__ == "__main__":
    _setup_commands()
    print("Bot started.")
    bot.infinity_polling()
