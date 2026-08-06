from app.bandwidth import (
    BandwidthMeter,
    capture_proxy_usage,
    record_proxy_request,
    record_proxy_response,
    record_proxy_retry,
)


def test_add_accumulates_bytes():
    meter = BandwidthMeter()
    meter.add(100)
    meter.add(50)
    assert meter.total_bytes == 150


def test_add_ignores_none():
    meter = BandwidthMeter()
    meter.add(None)
    assert meter.total_bytes == 0


def test_add_ignores_negative():
    meter = BandwidthMeter()
    meter.add(100)
    meter.add(-10)
    assert meter.total_bytes == 100


def test_add_ignores_zero():
    meter = BandwidthMeter()
    meter.add(0)
    assert meter.total_bytes == 0


def test_total_gb_math():
    meter = BandwidthMeter()
    meter.add(1024 ** 3)
    assert meter.total_gb == 1.0


def test_reset_zeroes_counter():
    meter = BandwidthMeter()
    meter.add(500)
    meter.reset()
    assert meter.total_bytes == 0


def test_capture_attributes_bytes_requests_and_retries_to_current_operation():
    with capture_proxy_usage() as usage:
        record_proxy_request(120)
        record_proxy_response(880)
        record_proxy_retry()

    assert usage.bytes_up == 120
    assert usage.bytes_down == 880
    assert usage.request_count == 1
    assert usage.retry_count == 1


def test_nested_capture_restores_parent_without_cross_contamination():
    with capture_proxy_usage() as parent:
        record_proxy_request(10)
        with capture_proxy_usage() as child:
            record_proxy_request(20)
            record_proxy_response(30)
        record_proxy_response(40)

    assert (parent.bytes_up, parent.bytes_down) == (10, 40)
    assert (child.bytes_up, child.bytes_down) == (20, 30)
