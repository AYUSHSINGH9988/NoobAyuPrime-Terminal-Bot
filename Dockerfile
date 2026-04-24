FROM python:3.10-slim

WORKDIR /app

# System tools
RUN apt-get update && apt-get install -y \
    git \
    wget \
    curl \
    ffmpeg \
    aria2 \
    zip \
    unzip \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot files
COPY . .

# Create runtime directories
RUN mkdir -p scripts logs venvs

# Koyeb uses port 8000 by default for web services
EXPOSE 8000

CMD ["python3", "-u", "bot.py"]
