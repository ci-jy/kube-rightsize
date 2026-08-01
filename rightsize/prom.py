"""Minimal Prometheus HTTP API client (query_range only)."""

import json
import urllib.parse
import urllib.request


class PrometheusError(RuntimeError):
    pass


class Prometheus:
    def __init__(self, base_url, timeout=60):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def query_range(self, query, start, end, step):
        """Run a range query; returns the raw ``data.result`` list.

        Each element is ``{"metric": {...}, "values": [[ts, "value"], ...]}``.
        """
        params = urllib.parse.urlencode(
            {"query": query, "start": f"{start:.3f}", "end": f"{end:.3f}", "step": f"{float(step):g}"}
        )
        req = urllib.request.Request(
            f"{self.base_url}/api/v1/query_range",
            data=params.encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise PrometheusError(f"{exc.code} from Prometheus: {detail}") from exc
        except urllib.error.URLError as exc:
            raise PrometheusError(f"cannot reach Prometheus at {self.base_url}: {exc.reason}") from exc
        if body.get("status") != "success":
            raise PrometheusError(body.get("error", "query failed"))
        return body["data"]["result"]
