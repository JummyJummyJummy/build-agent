FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ .

ENV REPORTS_DIR=/reports
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "server.py"]
