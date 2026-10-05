# Sandbox image for running Cosmic Python's tests.
# Contains only Python and the target's dependencies. The repository copy
# is mounted at /work when a check runs; it is never baked into the image.
FROM python:3.12-slim

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /work
