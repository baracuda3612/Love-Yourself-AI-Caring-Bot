FROM python:3.11-slim

# Встановлюємо залежності для SQLite
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsqlite3-0 \
    && rm -rf /var/lib/apt/lists/*

# Створюємо робочу директорію
WORKDIR /app

# Копіюємо залежності
COPY requirements.txt .

# Встановлюємо залежності
RUN pip install --no-cache-dir -r requirements.txt

# Копіюємо код
COPY . .

# Команда для запуску
CMD ["python", "-m", "app.main"]
