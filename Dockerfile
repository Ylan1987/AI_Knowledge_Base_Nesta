# Usamos una imagen de Python como base
FROM python:3.11-slim

# Variables de entorno
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Instalamos dependencias del sistema
RUN apt-get update && apt-get install -y --no-install-recommends 
    build-essential curl git cmake libsqlite3-dev python3-dev 
    && rm -rf /var/lib/apt/lists/*

# Copiamos el archivo de requerimientos y lo instalamos
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt

# --- MANEJO DE CREDENCIALES DE GOOGLE ---
# 1. Copiamos el archivo de credenciales a una ubicación estándar dentro de la imagen
COPY gcloud_credentials.json /app/gcloud_credentials.json
# 2. Establecemos la variable de entorno para que las librerías de Google lo encuentren
ENV GOOGLE_APPLICATION_CREDENTIALS=/app/gcloud_credentials.json
# ---

# Copiamos el resto del código de la aplicación
COPY . .

# Comando de inicio
CMD ["python", "run_daemon.py"]
