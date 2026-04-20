ARG BUILD_FROM
FROM ${BUILD_FROM}

# Upgrade base packages to avoid version conflicts, then install deps
RUN apk upgrade --no-cache \
    && apk add --no-cache \
        python3 py3-pip py3-wheel \
        py3-cryptography \
        libffi-dev \
        curl jq

WORKDIR /app

# Layer: dependencies (cached unless requirements change)
COPY requirements.txt .
# cryptography is already installed via apk (py3-cryptography), skip it in pip
RUN pip3 install --no-cache-dir --break-system-packages \
    $(grep -v '^cryptography' requirements.txt | tr '\n' ' ')

# Layer: application code
COPY app/ ./app/

# Layer: HA custom integration (installed to /config by run.sh)
COPY custom_components/ ./custom_components/

# Persistent data dir for SQLite DB
RUN mkdir -p /data

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
