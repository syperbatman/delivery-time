FROM python:3.11-slim

# логи сразу во вывод (иначе print буферизуется и его не видно в дашборде хостинга)
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ВАЖНО: база должна лежать на примонтированном томе, чтобы переживать редеплой.
# На хостинге примонтируй volume в /data и оставь этот путь.
ENV DB_PATH=/data/delivery.db

CMD ["python", "bot.py"]
