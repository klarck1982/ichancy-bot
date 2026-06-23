FROM python:3.11-slim

WORKDIR /app

# تثبيت اعتماديات النظام الأساسية (بدون خطوط Playwright الإضافية)
RUN apt-get update && apt-get install -y \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libwayland-client0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# تثبيت مكتبات بايثون
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# تثبيت Chromium للمتصفح بدون تبعيات إضافية
RUN playwright install chromium

# نسخ باقي الكود
COPY . .

# تشغيل البوت
CMD ["python", "main.py"]
