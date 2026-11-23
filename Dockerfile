FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RAG_DATA_DIR=/app/var
WORKDIR /app
ARG WITH_OCR=0
COPY requirements.txt requirements-ocr.txt ./
RUN if [ "$WITH_OCR" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
      && rm -rf /var/lib/apt/lists/* \
      && python -m pip install --no-cache-dir --disable-pip-version-check -r requirements-ocr.txt; \
    else \
      python -m pip install --no-cache-dir --disable-pip-version-check -r requirements.txt; \
    fi
RUN groupadd --system rag \
    && useradd --system --gid rag --home-dir /app/var rag \
    && install -d -o rag -g rag -m 0750 /app/var
COPY main.py .
COPY ragcore/ ragcore/
COPY profiles/ profiles/
COPY eval/ eval/
COPY web/ web/
RUN if [ "$WITH_OCR" = "1" ]; then \
      RAG_OCR_ENABLED=1 python -c "import cv2, onnxruntime, pypdfium2; from ragcore.ocr import available; assert available(), 'OCR components or model files are missing'"; \
    fi
USER rag:rag
EXPOSE 8781
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8781", "--no-proxy-headers"]
