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

try:
    from dotenv import load_dotenv  # локально читаем .env; на проде переменные задаёт хостинг
    load_dotenv()
except ImportError:
    pass

import db
from parser import extract_words_from_pdf, detect_report_kind, parse_words, seconds_to_time

TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise ValueError("❌ TOKEN not found! Положи токен бота в переменную окружения TOKEN.")

bot = telebot.TeleBot(TOKEN)
db.init_db()


# ─────────────────────────────────────────────────────────────────────────────
# СТАТУС/УРОВЕНЬ АККАУНТА — отложено. Это tier/очки (напр. "Silver 🥈 70"),
# зависит не только от среднего времени и различается по магазинам. Сюда же —
# будущие рекомендации курьеру (сколько ∅-доставок до нужного среднего, запас
# по времени, манипуляция доставка⇄выезд). Требует отдельной проработки.
# ─────────────────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["start"])
def cmd_start(message):
    bot.reply_to(
        message,
        "👋 Привет! Пересылай сюда свой ежедневный PDF-отчёт — я распознаю дату и "
        "время доставки, сохраню по дням и посчитаю среднее за неделю (Пн–Вс).\n\n"
        "Команды:\n"
        "/stats — среднее за текущую неделю и статус\n"
        "/reset — удалить все мои данные",
    )


@bot.message_handler(commands=["reset"])
def cmd_reset(message):
    n = db.delete_user_data(message.from_user.id)
    bot.send_message(message.chat.id, f"🗑 Удалено записей: {n}. Можно присылать отчёты заново.")


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    bot.send_message(message.chat.id, _format_all_weeks(message.from_user.id))


@bot.message_handler(content_types=["document"])
def handle_pdf(message):
    user_id = message.from_user.id
    doc = message.document
    fname = doc.file_name or ""

    if not fname.lower().endswith(".pdf"):
        bot.send_message(message.chat.id, "❌ Пришли, пожалуйста, PDF-файл отчёта.")
        return

    try:
        file_info = bot.get_file(doc.file_id)
        data = bot.download_file(file_info.file_path)
        words = extract_words_from_pdf(data)
    except Exception as e:  # noqa: BLE001
        bot.send_message(message.chat.id, f"❌ Не удалось прочитать PDF: {e}")
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
        bot.send_message(message.chat.id, f"❌ Ошибка сохранения: {e}")
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


def _week_block(w) -> str:
    """Одна календарная неделя в виде текстового блока."""
    return "\n".join([
        f"🗓 {date.fromisoformat(w.week_start).strftime('%d.%m')} — "
        f"{date.fromisoformat(w.week_end).strftime('%d.%m.%Y')}",
        f"⏱ Доставка: {seconds_to_time(w.avg_delivery_sec)}",
        f"🛵 Выезд: {seconds_to_time(w.avg_start_sec)}",
        f"📦 Заказов: {w.total_orders}  ·  💰 {w.total_earnings:.2f} zł",
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
    print("Bot started.")
    bot.infinity_polling()
