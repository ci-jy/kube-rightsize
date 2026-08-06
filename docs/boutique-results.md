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
