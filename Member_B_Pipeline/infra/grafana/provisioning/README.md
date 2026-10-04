# Optional Grafana setup (untested here)

No Docker, and Grafana/Prometheus are large downloads this project does not fetch for you. The built-in
dashboard at `http://127.0.0.1:8080/ops/` needs nothing. If you run your own Prometheus (config:
`infra/prometheus/prometheus.yml`) and Grafana:

1. Add a Prometheus data source with UID `prometheus` (the dashboard refers to that UID).
2. Import `infra/grafana/dashboards/fairdrop.json` (Dashboards, New, Import, Upload JSON).

`backend/tests_defence/test_observability.py` checks that every metric the dashboard queries exists in the
backend's registry, and that the JSON is well formed, so the file cannot silently drift from the code.
