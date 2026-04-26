FROM python:3.12-slim

# Install system dependencies for Python packages
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    build-essential \
    curl \
    gnupg \
    apt-transport-https \
    libssl-dev \
    libffi-dev \
    libjpeg-dev \
    zlib1g-dev \
    libblas-dev \
    liblapack-dev \
    libpq-dev \
    unixodbc \
    unixodbc-dev \
    gfortran \
    && rm -rf /var/lib/apt/lists/*

# Install Microsoft ODBC SQL Server driver (Linux runtime for pyodbc).
# Fallback to Debian 12 if version 13 (trixie) is detected, as Microsoft hasn't released 13 packages yet.
RUN set -eux; \
    apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg2; \
    . /etc/os-release; \
    export MSSQL_VER=$VERSION_ID; \
    if [ "$VERSION_ID" -ge "13" ] || [ "$VERSION_CODENAME" = "trixie" ]; then export MSSQL_VER=12; fi; \
    curl -sSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg; \
    echo "deb [arch=amd64,arm64,armhf signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/${MSSQL_VER}/prod bookworm main" > /etc/apt/sources.list.d/mssql-release.list; \
    apt-get update; \
    ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql17 msodbcsql18 unixodbc-dev; \
    rm -rf /var/lib/apt/lists/*

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set work directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Expose Django port
EXPOSE 8000

# Run Django app with gunicorn (production-ready)
# Note: Run migrations separately before starting (e.g., in CI/CD or entrypoint script)
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", "--timeout", "300", "--graceful-timeout", "60", "config.wsgi:application"]
