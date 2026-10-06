FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=10000

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY . ./
RUN DJANGO_DEBUG=1 python manage.py collectstatic --noinput

EXPOSE 10000

CMD ["sh", "-c", ": \"${DJANGO_SECRET_KEY:?DJANGO_SECRET_KEY must be set}\"; export DJANGO_DEBUG=0; python manage.py migrate --noinput && gunicorn ai_notes_summarizer.wsgi:application --bind 0.0.0.0:${PORT} --workers 1 --threads 4 --timeout 120"]
