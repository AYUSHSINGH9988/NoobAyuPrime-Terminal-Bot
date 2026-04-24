FROM python:3.10-slim

WORKDIR /app

# Server me basic tools daal rahe hain (neofetch hata diya kyunki ab wo dead hai)
RUN apt-get update && apt-get install -y \
    git \
    wget \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Koyeb ke liye port expose karna zaroori hai
EXPOSE 8080

CMD ["python3", "bot.py"]
