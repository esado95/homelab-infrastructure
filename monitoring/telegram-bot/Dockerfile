FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

ENV PYTHONUNBUFFERED=1

# Aucun port exposé : le bot appelle l'API Telegram (long polling),
# rien n'entre depuis l'extérieur.
CMD ["python", "app.py"]
