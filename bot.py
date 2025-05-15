
import telebot
import fitz  # PyMuPDF
import re
import os

TOKEN = os.environ.get('TOKEN')

if not TOKEN:
    raise ValueError("❌ TOKEN not found!")

bot = telebot.TeleBot(TOKEN)

user_data = {}

def time_to_seconds(time_str):
    minutes, seconds = map(int, time_str.strip().split(':'))
    return minutes * 60 + seconds

def seconds_to_time(seconds):
    minutes = seconds // 60
    sec = seconds % 60
    return f"{minutes}:{sec:02d}"

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "👋 Hi! Send me a PDF summary, and I'll calculate your average delivery time and earnings!")

@bot.message_handler(commands=['reset'])
def reset_data(message):
    user_id = message.from_user.id
    user_data[user_id] = {
        'total_delivery_seconds': 0,
        'total_start_seconds': 0,
        'total_earnings': 0,
        'delivery_orders': 0,
        'start_orders': 0,
        'files_uploaded': 0
    }
    bot.send_message(message.chat.id, "✅ Data reset. Ready for new uploads!")

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

    delivery_time = None
    start_time = None
    earnings = 0.0

    # Find delivery time
    if "Number of delayed orders" in text:
        delivery_area = text.split("Number of delayed orders", 1)[-1]
        match = re.search(r'(\d{1,2}:\d{2})', delivery_area)
        if match:
            delivery_time = match.group(1)

    # Find start time
    if "Shift Lifetime" in text:
        start_area = text.split("Shift Lifetime", 1)[-1]
        match = re.search(r'(\d{1,2}:\d{2})', start_area)
        if match:
            start_time = match.group(1)

    # Find earnings
    if "Total earnings" in text:
        earnings_area = text.split("Total earnings", 1)[-1]
        numbers = re.findall(r'(\d+[.,]?\d*)(\s*zł)?', earnings_area)
        seen_plain_number = False
        for value, is_zloty in numbers:
            if not is_zloty:
                seen_plain_number = True
            elif seen_plain_number and is_zloty:
                earnings = float(value.replace(',', '.'))
                break

    if not (delivery_time and start_time):
        bot.send_message(message.chat.id, "❌ Couldn't find delivery or start times in the file.")
        return

    delivery_seconds = time_to_seconds(delivery_time)
    start_seconds = time_to_seconds(start_time)

    task_hours = re.findall(r'jush\s+(\d{1,2}):00\s+(\d+)\s+', text)

    if not task_hours:
        bot.send_message(message.chat.id, "❌ Couldn't find order-by-hour table.")
        return

    delivery_orders = 0
    start_orders = 0

    for hour, orders in task_hours:
        hour = int(hour)
        orders = int(orders)
        if hour < 23:
            delivery_orders += orders
        start_orders += orders

    user_data[user_id]['total_delivery_seconds'] += delivery_seconds * delivery_orders
    user_data[user_id]['total_start_seconds'] += start_seconds * start_orders
    user_data[user_id]['total_earnings'] += earnings
    user_data[user_id]['delivery_orders'] += delivery_orders
    user_data[user_id]['start_orders'] += start_orders
    user_data[user_id]['files_uploaded'] += 1

    total_delivery_orders = user_data[user_id]['delivery_orders']
    total_start_orders = user_data[user_id]['start_orders']

    final_delivery = user_data[user_id]['total_delivery_seconds'] / total_delivery_orders if total_delivery_orders else 0
    final_start = user_data[user_id]['total_start_seconds'] / total_start_orders if total_start_orders else 0
    total_earnings = user_data[user_id]['total_earnings']

    result = f"""
🚀 AVERAGE DELIVERY TIME (before 23:00): {seconds_to_time(int(final_delivery))}
🚀 AVERAGE START TIME (all orders): {seconds_to_time(int(final_start))}
💰 TOTAL EARNINGS: {total_earnings:.2f} zł

📄 Files analyzed: {user_data[user_id]['files_uploaded']}
📦 Total delivery orders: {total_delivery_orders}
📦 Total start orders: {total_start_orders}
"""

    bot.send_message(message.chat.id, result)

bot.infinity_polling()
