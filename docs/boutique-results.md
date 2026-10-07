# Online Boutique results

From `recordings/boutique-30m.json`: 30 minutes of the `boutique` namespace on the kind cluster, recorded after a 2-minute warm-up with the load generator at its default 10 users, and with `recommendationservice` squeezed to a 40Mi limit.

| Container | Requested CPU / mem | Observed P95 CPU / P99 mem | Recommended CPU / mem | Mem limit | Flags |
|---|---|---|---|---|---|
| adservice | 200m / 180Mi | 11.8m / 142.7Mi | 15m / 160Mi | 200Mi | - |
| cartservice | 200m / 64Mi | 8.8m / 50.0Mi | 10m / 56Mi | 72Mi | - |
| checkoutservice | 100m / 64Mi | 0.9m / 8.3Mi | 10m / 32Mi | 40Mi | - |
| currencyservice | 100m / 64Mi | 10.6m / 38.8Mi | 15m / 44Mi | 56Mi | - |
| emailservice | 100m / 64Mi | 1.7m / 43.4Mi | 10m / 48Mi | 60Mi | - |
| frontend | 100m / 64Mi | 17.5m / 12.1Mi | 20m / 32Mi | 40Mi | - |
| loadgenerator | 300m / 256Mi | 5.4m / 46.6Mi | 10m / 52Mi | 68Mi | - |
| paymentservice | 100m / 64Mi | 1.3m / 28.7Mi | 10m / 32Mi | 40Mi | - |
| productcatalogservice | 100m / 64Mi | 8.9m / 9.3Mi | 10m / 32Mi | 40Mi | - |
| recommendationservice | 100m / 32Mi | 8.9m / 41.2Mi | 10m / 140Mi | 176Mi | memory-censored |
| redis-cart | 70m / 200Mi | 2.8m / 6.1Mi | 10m / 32Mi | 40Mi | - |
| shippingservice | 100m / 64Mi | 1.2m / 8.4Mi | 10m / 32Mi | 40Mi | - |

Totals: requested CPU 1570m to 140m (91.1% freed), requested memory 1180Mi to 692Mi (41.4% freed).

The memory total counts `recommendationservice` at its squeezed 32Mi request. The stock manifest asks for 220Mi, so against the unmodified manifest the saving would be larger.

Several services sit at the 10m CPU or 32Mi memory floor, so for them the recommendation really means "as small as it is willing to go".

## Recommendations against the patched replay

The run behind the report ([results/boutique-validate.json](../results/boutique-validate.json)) also measured what each container actually used during the patched replay, so the new requests can be checked against it:

| container | recommended memory request | peak in replay | recommended CPU request | average CPU in replay |
|---|---|---|---|---|
| adservice | 160Mi | 105.8Mi | 15m | 12.5m |
| cartservice | 56Mi | 44.4Mi | 10m | 13.6m |
| currencyservice | 44Mi | 30.3Mi | 15m | 13.8m |
| emailservice | 48Mi | 55.7Mi | 10m | 1.6m |
| recommendationservice | 140Mi | 57.3Mi | 10m | 7.9m |

Memory stayed under every new limit. emailservice peaked a little above its request but well under its 60Mi limit. cartservice averaged 13.6m of CPU against a 10m request. That's allowed (requests aren't caps, and its CPU limit stayed), but it suggests P95 of a 30-minute recording is on the low side for it.

## Other known gaps

- The cAdvisor OOM fallback needs `/dev/kmsg`, which my VM didn't have, so there it always read zero and only kube-state-metrics' `last_terminated_reason` is used.
- `rightsize.yml` has been linted and the same script run locally, but not yet on GitHub's runners. The squeezed service never becomes available, so `up.sh` sits out its full 15-minute wait and the whole job takes over half an hour.
