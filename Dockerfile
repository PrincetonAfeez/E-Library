FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN adduser --disabled-password --gecos "" appuser
COPY . /app/
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .
RUN python manage.py collectstatic --noinput

USER appuser

CMD ["gunicorn", "elibrary.wsgi:application", "--bind", "0.0.0.0:8000"]
