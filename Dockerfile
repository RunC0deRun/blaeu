FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app

# Install system dependencies (including FFmpeg for video transcoding)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Copy requirements first to leverage caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app.py db.py gpx_parser.py ./
COPY static/ ./static/
COPY templates/ ./templates/

# Create the persistence directory
RUN mkdir -p /data

EXPOSE 5000

# Run with gunicorn
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]
