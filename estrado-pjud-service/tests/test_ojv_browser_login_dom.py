"""Offline Chromium regression for the observed OJV accessibility mismatch.

Run with PJUD_RUN_BROWSER_TESTS=1 and the pinned Playwright browser installed.
All requests are intercepted; no provider, credentials, or existing profile is used.
"""
from __future__ import annotations

import os
import time

import pytest
from playwright.async_api import async_playwright
from pydantic import SecretStr

from app.ojv import browser_login
from app.ojv.errors import FamiliaBlockedError, OjvTimeoutError, OjvUpstreamChangedError


pytestmark = pytest.mark.skipif(
    os.environ.get("PJUD_RUN_BROWSER_TESTS") != "1",
    reason="explicit offline browser test opt-in required",
)


@pytest.mark.parametrize("aria_hidden,buttons,success,captcha", [
    ("true", '<button>Ingresar</button>', True, ""),
    ("false", '<button>Ingresar</button>', True, ""),
    ("true", '<button style="display:none">Ingresar</button>', False, ""),
    ("true", '<button>Ingresar</button><button>Ingresar</button>', False, ""),
    ("true", '<button>Ingresar</button><button style="display:none">Ingresar</button>', True, ""),
    ("true", '<button>Ingresar</button>', True, "badge"),
    ("true", '<button>Ingresar</button>', False, "challenge"),
])
async def test_official_login_requires_one_physically_visible_submit_in_own_form(
    monkeypatch: pytest.MonkeyPatch, aria_hidden: str, buttons: str, success: bool, captcha: str,
) -> None:
    # Regression: default role queries exclude descendants of aria-hidden="true",
    # even when their CSS visibility is normal. Mock locators missed this in OJV.
    entry = "https://oficinajudicialvirtual.pjud.cl/home/index.php"
    landing = "https://oficinajudicialvirtual.pjud.cl/indexN.php"
    html = f"""<meta charset="utf-8"><button>Todos los servicios</button>
      <a href="#" onclick="document.querySelector('#segunda-clave-access').style.display='block';return false">Clave Poder Judicial</a>
      <button>Ingresar</button><!-- A visible decoy outside the credential form. -->
      <div id="segunda-clave-access" aria-hidden="{aria_hidden}" style="display:none">
        <form id="fSGN" action="/indexN.php" method="post">
          <input type="text" name="rut" placeholder="Ingrese su Rut sin dígito verificador, Ej: 12345678">
          <input type="password" name="password">{buttons}
        </form>
      </div>"""
    if captcha:
        # A visible badge is present on the real entry page before authentication.
        # Delay navigation to exercise the actual post-submit classifier.
        html += '''<div class="grecaptcha-badge"><div class="grecaptcha-logo">
          <iframe title="reCAPTCHA" srcdoc="" width="256" height="60"></iframe>
          </div></div><script>
          document.querySelector('#fSGN').addEventListener('submit', event => {
            event.preventDefault();
            setTimeout(() => document.querySelector('#fSGN').submit(), 350);
          });</script>'''
        if captcha == "challenge":
            html += '<iframe title="recaptcha challenge" srcdoc="" width="300" height="300"></iframe>'
    submissions: list[str | None] = []
    unexpected: list[str] = []

    async with async_playwright() as runtime:
        original_launch = runtime.chromium.launch

        async def launch(**kwargs):
            # Headless only for this synthetic fixture, never the production flow.
            kwargs["headless"] = True
            browser = await original_launch(**kwargs)
            original_context = browser.new_context

            async def new_context(**context_kwargs):
                context = await original_context(**context_kwargs, service_workers="block")

                async def route_request(route):
                    request = route.request
                    if request.url == entry and request.method == "GET":
                        await route.fulfill(status=200, content_type="text/html", body=html)
                    elif request.url == landing and request.method == "POST":
                        submissions.append(request.post_data)
                        await route.fulfill(status=200, headers={
                            "Content-Type": "text/html",
                            "Set-Cookie": "AUTH=synthetic; Secure; Path=/; HttpOnly",
                        }, body='<a href="#infousuario">Cuenta sintética</a><a href="#">Mis Causas</a>')
                    else:
                        unexpected.append(request.method)
                        await route.abort()

                await context.route("**/*", route_request)
                return context

            monkeypatch.setattr(browser, "new_context", new_context)
            return browser

        class BorrowedRuntime:
            async def __aenter__(self):
                return runtime

            async def __aexit__(self, *_args):
                return None

        monkeypatch.setattr(runtime.chromium, "launch", launch)
        monkeypatch.setattr(browser_login, "async_playwright", BorrowedRuntime)
        monkeypatch.setattr(browser_login, "_LOGIN_TIMEOUT_S", 5.0)
        if success:
            result = await browser_login.login_official_ojv(
                SecretStr("11.111.111-1"), SecretStr("synthetic-password"),
                proxy_url=None, user_agent="offline-regression",
            )
            assert [(cookie.name, cookie.value) for cookie in result.cookies] == [("AUTH", "synthetic")]
            assert submissions == ["rut=11111111&password=synthetic-password"]
        else:
            expected_errors = (FamiliaBlockedError,) if captcha == "challenge" else (OjvTimeoutError, OjvUpstreamChangedError)
            with pytest.raises(expected_errors):
                await browser_login.login_official_ojv(
                    SecretStr("11.111.111-1"), SecretStr("synthetic-password"),
                    proxy_url=None, user_agent="offline-regression",
                )
            assert submissions == []
        assert unexpected == []


@pytest.mark.parametrize("account_html,menu_html,account_shape,menu_shape", [
    ('<a href="#infousuario">Synthetic account</a>', '<a href="#">Mis Causas</a>', (1, 1), (1, 1)),
    ('<a href="#infousuario" style="display:none">Synthetic account</a>', '<a href="#">Mis Causas</a>', (1, 0), (1, 1)),
    ('', '<a href="#">Mis Causas</a><a href="#">Mis Causas</a>', (0, 0), (2, 2)),
])
async def test_landing_probe_reads_real_dom_without_exposing_page_contents(
    account_html, menu_html, account_shape, menu_shape,
) -> None:
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            context = await browser.new_context(service_workers="block")
            page = await context.new_page()
            probe = browser_login._LandingProbe()
            page.on("response", lambda response: browser_login._record_landing_response(page, probe, response))
            await context.route("**/*", lambda route: route.fulfill(
                status=200, content_type="text/html", body=account_html + menu_html,
            ))
            await page.goto("https://oficinajudicialvirtual.pjud.cl/indexN.php")
            await browser_login._sample_landing(page, probe, time.monotonic() + 2)
            assert probe.main_http == 200
            assert probe.ready == "complete"
            assert probe.account == account_shape
            assert probe.my_causes == menu_shape
            assert "Synthetic account" not in repr(probe)
            assert "https" not in repr(probe)
        finally:
            await browser.close()
