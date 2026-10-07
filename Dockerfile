# The rightsize CLI on its own: record, recommend, patch and report.
# `validate` also needs kubectl and a cluster, so it is run from the host or CI.
# The package has no runtime dependencies, so it is copied in rather than
# pip-installed. There are no RUN steps, so the build needs no network beyond
# pulling the base image and never has to start a container (which also keeps
# it working on hosts where runc can't).
FROM python:3.12-slim

COPY rightsize /opt/rightsize/rightsize
ENV PYTHONPATH=/opt/rightsize PYTHONDONTWRITEBYTECODE=1

USER 10001
WORKDIR /work
ENTRYPOINT ["python", "-m", "rightsize"]
CMD ["--help"]
