ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install Python 3 + pip + build deps for cryptography (hassio base is Alpine)
RUN apk add --no-cache \
        python3 py3-pip py3-wheel \
        gcc musl-dev libffi-dev openssl-dev cargo \
        curl jq \
    && python3 -m ensurepip --upgrade

WORKDIR /app

# Layer: dependencies (cached unless requirements change)
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Layer: application code
COPY app/ ./app/

# Layer: HA custom integration (installed to /config by run.sh)
COPY custom_components/ ./custom_components/

# Persistent data dir for SQLite DB
RUN mkdir -p /data

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
