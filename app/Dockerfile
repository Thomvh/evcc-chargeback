FROM python:3.12.16-slim-trixie
# The Supervisor builds this folder only, so the tool comes from the release of the same version.
RUN pip install --no-cache-dir https://github.com/Thomvh/evcc-chargeback/archive/refs/tags/v0.3.0.tar.gz
ENV EVCC_CHARGEBACK_OPTIONS=/data/options.json OUTPUT_DIR=/share/evcc-chargeback STATE_DIR=/data/state
ENTRYPOINT ["evcc-chargeback"]
CMD ["--schedule"]
