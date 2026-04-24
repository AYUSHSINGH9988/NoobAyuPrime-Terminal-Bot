FROM python:3.10-slim

WORKDIR /app

# Server me basic tools daal rahe hain taaki tera Termux wala feel aaye
RUN apt-get update && apt-get install -y \
    git \
    wget \
    curl \
    neofetch \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python3", "bot.py"]
