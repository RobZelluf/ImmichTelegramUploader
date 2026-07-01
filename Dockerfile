FROM python:3.13-slim

# No byte-code writes, unbuffered logs for clean `docker logs` output.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install deps first so this layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 appuser
USER appuser

CMD ["python", "bot.py"]
