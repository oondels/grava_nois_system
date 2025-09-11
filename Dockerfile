FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /usr/src/app

# OS deps: ffmpeg (required).
# pigpio daemon is optional and not available on Debian trixie repos.
# If you need GPIO inside the container, run pigpio on the host
# and expose it (see docker-compose comments), or use a base image
# where pigpio exists (e.g., Debian bookworm) and install it there.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

COPY gn_start.sh .
# RUN echo "--- Verificando o conteúdo de entrypoint.sh no momento do build: ---" && cat ./entrypoint.sh && echo "----------------------------------------------------"
CMD ["./gn_start.sh"]
