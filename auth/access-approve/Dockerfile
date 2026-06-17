FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

ENV PYTHONUNBUFFERED=1
EXPOSE 8090

# 1 воркер (чтобы уведомитель был в единственном экземпляре) + потоки на запросы
CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:8090", "app:app"]
