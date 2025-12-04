# Используем официальный Python образ
FROM python:3.10-slim

# Устанавливаем рабочую директорию в контейнере
WORKDIR /app

# Сначала копируем requirements (так сборка быстрее)
COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь проект внутрь контейнера
COPY . .

# Запускаем бота
CMD ["python", "bot.py"]
