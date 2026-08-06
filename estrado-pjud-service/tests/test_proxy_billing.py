import httpx

from app.proxy_billing import is_proxy_billing_error


def test_detects_iproyal_402_proxy_error():
    assert is_proxy_billing_error(httpx.ProxyError("407 tunnel failed: 402 Payment Required"))


def test_detects_nested_proxy_billing_error():
    outer = RuntimeError("request failed")
    outer.__cause__ = httpx.ProxyError("402 payment required")
    assert is_proxy_billing_error(outer)


def test_does_not_confuse_pjud_http_402_with_proxy_billing():
    request = httpx.Request("GET", "https://oficinajudicialvirtual.pjud.cl")
    response = httpx.Response(402, request=request)
    error = httpx.HTTPStatusError("402", request=request, response=response)
    assert is_proxy_billing_error(error) is False


def test_generic_proxy_failure_is_not_billing():
    assert is_proxy_billing_error(httpx.ProxyError("proxy connection refused")) is False
