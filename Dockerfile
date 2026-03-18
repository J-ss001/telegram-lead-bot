FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY telegram_lead_bot.py .
CMD ["python", "telegram_lead_bot.py"]
