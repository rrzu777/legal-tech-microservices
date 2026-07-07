from app.bandwidth import BandwidthMeter


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
