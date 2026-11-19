FROM python:3.12.16-slim-trixie
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --system --uid 10001 billing && mkdir /data && chown billing /data
USER billing
ENV OUTPUT_DIR=/data TZ=Europe/Amsterdam
VOLUME /data
ENTRYPOINT ["evcc-chargeback"]
CMD ["--schedule"]
