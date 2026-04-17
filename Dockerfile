FROM python:3.12-slim

# Install ffmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py transcriber.py database.py auth.py auth_routes.py sms.py email_service.py ./
COPY templates/ templates/
COPY static/ static/

# Create data directories
RUN mkdir -p uploads audio data

EXPOSE 8000

# Run with 0.0.0.0 to accept connections from outside the container
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
