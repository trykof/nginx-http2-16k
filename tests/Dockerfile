ARG IMAGE=cresset-nginx:test
FROM ${IMAGE}
RUN apk add --no-cache python3 py3-pip openssl \
    && python3 -m venv /testenv
COPY tests/requirements.txt /tests/requirements.txt
RUN /testenv/bin/pip install --no-cache-dir -r /tests/requirements.txt
COPY tests/protocol.py /tests/protocol.py
ENTRYPOINT ["/testenv/bin/python", "/tests/protocol.py"]
