FROM python:3.11-slim

WORKDIR /app

# تثبيت اعتماديات النظام لـ Playwright
RUN apt-get update && apt-get install -y \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libdbus-1-3 libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

# تثبيت المكتبات
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# تثبيت متصفح Chromium لـ Playwright (مع التبعيات)
RUN playwright install --with-deps chromium

# نسخ الكود
COPY . .

# تشغيل التطبيق
CMD ["python", "main.py"]