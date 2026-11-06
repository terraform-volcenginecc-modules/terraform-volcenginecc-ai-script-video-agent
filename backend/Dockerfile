FROM python:3.12.10-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --index-url https://mirrors.ivolces.com/pypi/simple/ --no-cache-dir -r requirements.txt
COPY backend/app ./app
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
