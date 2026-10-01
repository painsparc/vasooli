FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
# API mode (GET /health, POST /run). One-shot demo instead: docker run <img> python main.py
CMD ["python", "main.py", "--serve"]
