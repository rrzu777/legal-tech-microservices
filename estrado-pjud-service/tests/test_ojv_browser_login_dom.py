"""Offline Chromium regression for the observed OJV accessibility mismatch.

Run with PJUD_RUN_BROWSER_TESTS=1 and the pinned Playwright browser installed.
All requests are intercepted; no provider, credentials, or existing profile is used.
"""
from __future__ import annotations

import os

import pytest
from playwright.async_api import async_playwright
from pydantic import SecretStr

from app.ojv import browser_login
from app.ojv.errors import OjvTimeoutError, OjvUpstreamChangedError


pytestmark = pytest.mark.skipif(
    os.environ.get("PJUD_RUN_BROWSER_TESTS") != "1",
    reason="explicit offline browser test opt-in required",
)


@pytest.mark.parametrize("aria_hidden,buttons,success", [
    ("true", '<button>Ingresar</button>', True),
    ("false", '<button>Ingresar</button>', True),
    ("true", '<button style="display:none">Ingresar</button>', False),
    ("true", '<button>Ingresar</button><button>Ingresar</button>', False),
    ("true", '<button>Ingresar</button><button style="display:none">Ingresar</button>', True),
])
async def test_official_login_requires_one_physically_visible_submit_in_own_form(
    monkeypatch: pytest.MonkeyPatch, aria_hidden: str, buttons: str, success: bool,
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
            with pytest.raises((OjvTimeoutError, OjvUpstreamChangedError)):
                await browser_login.login_official_ojv(
                    SecretStr("11.111.111-1"), SecretStr("synthetic-password"),
                    proxy_url=None, user_agent="offline-regression",
                )
            assert submissions == []
        assert unexpected == []
