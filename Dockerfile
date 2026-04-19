FROM python:3.11-slim@sha256:233de06753d30d120b1a3ce359d8d3be8bda78524cd8f520c99883bfe33964cf

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# Install dependencies
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

# Copy application runtime files only
COPY app.py ./
COPY templates ./templates
COPY static ./static

EXPOSE 8080

RUN addgroup --system app && adduser --system --ingroup app app \
    && chown -R app:app /app

USER app

# Honor $PORT if provided (Replit/heroku-style), fallback 8080
CMD ["/bin/sh", "-c", "exec gunicorn -b 0.0.0.0:${PORT:-8080} app:app"]
