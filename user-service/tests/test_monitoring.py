from fastapi.testclient import TestClient


def test_metrics_report_real_requests_without_raw_url_labels(
    client: TestClient,
):
    assert client.get("/health").status_code == 200
    assert client.get(
        "/private-user-name?username=secret-user"
    ).status_code == 404

    client.request("CUSTOM_UNBOUNDED_METHOD", "/unknown")
    response = client.get("/metrics")

    assert response.status_code == 200
    metrics = response.text
    assert (
        'user_service_http_requests_total{method="GET",route="/health",status="200"}'
        in metrics
    )
    assert (
        'user_service_http_request_duration_seconds_count{method="GET",route="/health"}'
        in metrics
    )
    assert (
        'user_service_http_requests_total{method="GET",route="unmatched",status="404"}'
        in metrics
    )
    assert "private-user-name" not in metrics
    assert "secret-user" not in metrics
    assert 'route="/metrics"' not in metrics
    assert 'method="OTHER"' in metrics
    assert "CUSTOM_UNBOUNDED_METHOD" not in metrics
    counter = next(line for line in metrics.splitlines() if line.startswith(
        'user_service_http_requests_total{method="GET",route="/health",status="200"}'
    ))
    assert float(counter.rsplit(" ", 1)[1]) >= 1
