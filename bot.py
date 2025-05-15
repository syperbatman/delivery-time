
import telebot
import fitz  # PyMuPDF
import re
import io
import os

TOKEN = os.environ.get('TOKEN')

if not TOKEN:
    raise ValueError("❌ TOKEN не найден! Проверь, задана ли переменная окружения 'TOKEN'.")

bot = telebot.TeleBot(TOKEN)

user_data = {}

def time_to_seconds(time_str):
    minutes, seconds = map(int, time_str.split(':'))
    return minutes * 60 + seconds

def seconds_to_time(seconds):
    minutes = seconds // 60
    sec = seconds % 60
    return f"{minutes}:{sec:02d}"

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "👋 Cześć! Wyślij mi plik PDF z podsumowaniem dostaw, a ja policzę średni czas i zarobek!")

@bot.message_handler(commands=['reset'])
def reset_data(message):
    user_id = message.from_user.id
    if user_id in user_data:
        user_data[user_id] = {
            'total_delivery_seconds': 0,
            'total_start_seconds': 0,
            'total_earnings': 0,
            'delivery_orders': 0,
            'start_orders': 0,
            'files_uploaded': 0
        }
    bot.send_message(message.chat.id, "✅ Wszystkie dane zostały wyczyszczone. Możesz wysłać nowe pliki!")

@bot.message_handler(content_types=['document'])
def handle_pdf(message):
    user_id = message.from_user.id
    if user_id not in user_data:
        user_data[user_id] = {
            'total_delivery_seconds': 0,
            'total_start_seconds': 0,
            'total_earnings': 0,
            'delivery_orders': 0,
            'start_orders': 0,
            'files_uploaded': 0
        }

    file_info = bot.get_file(message.document.file_id)
    downloaded_file = bot.download_file(file_info.file_path)

    pdf = fitz.open(stream=downloaded_file, filetype="pdf")
    text = ""
    for page in pdf:
        text += page.get_text()

    # Теперь ищем по польским названиям
    delivery_match = re.search(r'Średni czas dostawy\s+(\d{1,2}:\d{2})', text)
    start_match = re.search(r'Średni czas wyjazdu\s+(\d{1,2}:\d{2})', text)

    if not (delivery_match and start_match):
        bot.send_message(message.chat.id, "❌ Nie udało się znaleźć czasu dostawy lub wyjazdu w pliku.")
        return

    delivery_time = delivery_match.group(1)
    start_time = start_match.group(1)

    delivery_seconds = time_to_seconds(delivery_time)
    start_seconds = time_to_seconds(start_time)

    task_hours = re.findall(r'jush\s+(\d{1,2}):00\s+(\d+)\s+', text)

    if not task_hours:
        bot.send_message(message.chat.id, "❌ Nie udało się znaleźć tabeli zamówień według godzin.")
        return

    delivery_orders = 0
    start_orders = 0

    for hour, orders in task_hours:
        hour = int(hour)
        orders = int(orders)
        if hour < 23:
            delivery_orders += orders
        start_orders += orders

    earnings_match = re.search(r'Suma zarobków\s+(\d+[.,]?\d*)\s*zł', text)
    if earnings_match:
        earnings = float(earnings_match.group(1).replace(',', '.'))
    else:
        earnings = 0.0

    user_data[user_id]['total_delivery_seconds'] += delivery_seconds * delivery_orders
    user_data[user_id]['total_start_seconds'] += start_seconds * start_orders
    user_data[user_id]['total_earnings'] += earnings
    user_data[user_id]['delivery_orders'] += delivery_orders
    user_data[user_id]['start_orders'] += start_orders
    user_data[user_id]['files_uploaded'] += 1

    total_delivery_orders = user_data[user_id]['delivery_orders']
    total_start_orders = user_data[user_id]['start_orders']

    if total_delivery_orders > 0:
        final_delivery = user_data[user_id]['total_delivery_seconds'] / total_delivery_orders
    else:
        final_delivery = 0

    if total_start_orders > 0:
        final_start = user_data[user_id]['total_start_seconds'] / total_start_orders
    else:
        final_start = 0

    total_earnings = user_data[user_id]['total_earnings']

    result = f"""
ŚREDNI CZAS DOSTAWY (do 23:00): {seconds_to_time(int(final_delivery))}
ŚREDNI CZAS WYJAZDU (wszystkie zamówienia): {seconds_to_time(int(final_start))}
ŁĄCZNE ZAROBKI: {total_earnings:.2f} zł

Przeanalizowano plików: {user_data[user_id]['files_uploaded']}
Łączna liczba zamówień dostawy: {total_delivery_orders}
Łączna liczba zamówień wyjazdu: {total_start_orders}
"""

    bot.send_message(message.chat.id, result)

bot.infinity_polling()
