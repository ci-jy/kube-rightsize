# The rightsize CLI on its own: record, recommend, patch and report.
# `validate` also needs kubectl and a cluster, so it is run from the host or CI.
# The package has no runtime dependencies, so it is copied in rather than
# pip-installed; the build needs no network beyond pulling the base image.
FROM python:3.12-slim

COPY rightsize /opt/rightsize/rightsize
ENV PYTHONPATH=/opt/rightsize PYTHONDONTWRITEBYTECODE=1

RUN useradd --create-home --uid 10001 rightsize
USER rightsize
WORKDIR /work
ENTRYPOINT ["python", "-m", "rightsize"]
CMD ["--help"]
