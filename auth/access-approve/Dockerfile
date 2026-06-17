FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

ENV PYTHONUNBUFFERED=1
EXPOSE 8090

# 1 worker (pour que le notificateur reste en exemplaire unique) + threads pour les requêtes
CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:8090", "app:app"]
