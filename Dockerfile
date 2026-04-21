FROM python:3.12-slim

# Install ffmpeg + WeasyPrint system deps (pango/cairo for PDF rendering)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 libffi-dev \
        fonts-liberation fonts-dejavu-core && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py transcriber.py database.py auth.py auth_routes.py sms.py email_service.py \
     chat_routes.py retrieval.py email_domains.py domain_routes.py billing_routes.py \
     integrations_routes.py zoom_provider.py ./
COPY templates/ templates/
COPY static/ static/

# Create data directories
RUN mkdir -p uploads audio data

EXPOSE 8000

# Run with 0.0.0.0 to accept connections from outside the container
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
