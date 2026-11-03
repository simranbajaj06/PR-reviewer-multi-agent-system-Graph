FROM python:3.11-slim

# Unbuffered so the worker's print() lines show up in `docker compose logs` immediately.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Imports are "src.pr_reviewer...." so /app must be the parent of src/.
COPY src ./src

RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 8000
# API by default; the worker service overrides this command in docker-compose.yml
CMD ["uvicorn", "src.pr_reviewer.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]