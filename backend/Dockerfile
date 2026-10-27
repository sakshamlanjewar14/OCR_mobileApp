FROM python:3.12-slim

# Keep the default image lean. Set INSTALL_TRANSLATION=1 only when enabling
# the optional local M2M100 translation model on a persistent host disk.
ARG INSTALL_TRANSLATION=0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-hin \
        tesseract-ocr-mar \
        ca-certificates \
        wget \
    && rm -rf /var/lib/apt/lists/*

# Prefer the lightweight official traineddata files when the build network
# allows it, but never fail a deployment because GitHub is unavailable. The
# language packages installed above are a working fallback.
RUN tessdata_dir="$(tesseract --print-tessdata-dir)" \
    && for language in eng hin mar; do \
         temporary="${tessdata_dir}/${language}.traineddata.fast"; \
         if wget -q --timeout=15 --tries=2 \
           "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/${language}.traineddata" \
           -O "${temporary}" && test -s "${temporary}"; then \
           mv "${temporary}" "${tessdata_dir}/${language}.traineddata"; \
         else \
           rm -f "${temporary}"; \
           echo "Using packaged ${language} traineddata"; \
         fi; \
       done

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY requirements-translation.txt ./requirements-translation.txt
RUN if [ "$INSTALL_TRANSLATION" = "1" ]; then \
      pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
      && pip install --no-cache-dir -r requirements-translation.txt; \
    fi

COPY app ./app
COPY scripts ./scripts

# Liveness only: dependency readiness is exposed separately at /api/ready.
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.getenv('PORT','10000'), timeout=2)" || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000} --workers ${WEB_CONCURRENCY:-1} --timeout-keep-alive 10"]
