FROM python:3.11-slim

WORKDIR /app

# Install deps in a separate layer for better cache reuse.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source only (see .dockerignore).
COPY src/ ./src/

# Cloud Run injects PORT at runtime; 8080 is the documented default.
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn src.app:app --host 0.0.0.0 --port ${PORT}"]
