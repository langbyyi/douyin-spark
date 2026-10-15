from __future__ import annotations

import mimetypes
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from dyspark.config import BrowserOptions
from dyspark.login import LoginPage, LoginPhase
from dyspark.runtime import browser_executable


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        executable = browser_executable(BrowserOptions())
        executable = executable or shutil.which("chromium") or shutil.which("chromium-browser")
        executable = Path(executable or playwright.chromium.executable_path)
        if not executable.is_file():
            pytest.skip("browser integration tests require installed Chrome or Chromium")
        instance = playwright.chromium.launch(headless=True, executable_path=str(executable))
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    yield context.new_page()
    context.close()


@pytest.mark.parametrize("embedded", [False, True])
@pytest.mark.parametrize("dialog_role", ['role="dialog"', ""])
def test_bound_phone_hint_is_read_before_requesting_sms(page, embedded, dialog_role):
    verification = """
      <section DIALOG_ROLE>
        <h2>验证身份</h2><p>请使用绑定手机 138****0000 完成短信验证</p>
        <div><button>接收短信验证码</button></div>
      </section>
    """.replace("DIALOG_ROLE", dialog_role)
    page.set_content("<p>背景资料：绑定手机 139****1111</p>")
    if embedded:
        page.evaluate(
            "html => { const frame = document.createElement('iframe'); "
            "frame.srcdoc = html; document.body.append(frame); }",
            verification,
        )
        page.frame_locator("iframe").get_by_role("button").wait_for()
    else:
        page.locator("body").evaluate(
            "(body, html) => body.insertAdjacentHTML('beforeend', html)", verification
        )
    login = LoginPage(page)
    assert login.phase() is LoginPhase.SMS
    assert login.sms_choice_visible()
    assert login.phone_hint() == "138****0000"


@pytest.mark.parametrize("dialog_role", ['role="dialog"', ""])
def test_choice_without_phone_does_not_read_background_account(page, dialog_role):
    page.set_content(
        """
      <main><p>绑定手机 139****1111</p>
        <section DIALOG_ROLE><h2>验证身份</h2>
          <div><button>接收短信验证码</button></div>
        </section>
      </main>
    """.replace("DIALOG_ROLE", dialog_role)
    )
    assert LoginPage(page).phone_hint() is None


def test_phone_hint_ignores_input_values_and_hidden_verification(page):
    page.set_content("""
      <section role="dialog" style="display:none">
        <p>绑定手机 139****1111</p><button>接收短信验证码</button>
      </section>
      <section role="dialog"><p>请完成短信验证，绑定手机尾号0000</p>
        <input type="tel" value="13800000000">
        <input placeholder="短信验证码"><button>验证</button>
      </section>
    """)
    assert LoginPage(page).phone_hint() == "尾号 0000"


@pytest.mark.parametrize("dialog_role", ['role="dialog"', ""])
def test_phone_hint_survives_switch_to_sms_form_without_changing_action_scope(page, dialog_role):
    page.set_content(
        """
      <section DIALOG_ROLE>
        <h2>验证身份</h2><p>绑定手机 138****0000</p>
        <div id="method"><button onclick="document.querySelector('#method').hidden=true;
          document.querySelector('#sms').hidden=false">接收短信验证码</button></div>
        <form id="sms" hidden onsubmit="event.preventDefault(); window.verified=true">
          <p>请完成短信验证</p><input placeholder="验证码"><button>验证</button>
        </form>
        <form onsubmit="event.preventDefault(); window.wrongForm=true">
          <button>验证</button>
        </form>
      </section>
      """.replace("DIALOG_ROLE", dialog_role)
    )
    login = LoginPage(page)
    assert login.phone_hint() == "138****0000"
    login.request_code(None)
    assert not login.sms_choice_visible()
    assert login.phone_hint() == "138****0000"
    login.submit_code("123456")
    assert page.evaluate("window.verified === true && window.wrongForm !== true")
    assert page.locator("#sms input").input_value() == "123456"


def test_reading_phone_hint_does_not_expand_sms_choice_input_scope(page):
    page.set_content("""
      <section role="dialog"><h2>验证身份</h2><p>绑定手机 138****0000</p>
        <div><button>接收短信验证码</button></div>
        <form><input type="tel" placeholder="其他登录方式的手机号"></form>
      </section>
    """)
    login = LoginPage(page)
    assert login.phone_hint() == "138****0000"
    assert not login.phone_input_visible()


def open_panel(page, status="pending"):
    static = Path(__file__).parents[1] / "src" / "dyspark" / "gui_static"
    session = {
        "session_id": "test-session",
        "account": "test-account",
        "status": status,
        "new_account": True,
        "proxy_ref": "TEST_PROXY",
        "phone_input": False,
        "secondary_verification": status == "sms_required",
        "phone_hint": "138****0000",
        "qr_path": "qr.png",
        "qr_version": 1,
        "job_state": "running",
    }
    state = {"posts": [], "errors": []}
    page.on("pageerror", lambda error: state["errors"].append(str(error)))

    def route(request):
        if urlsplit(request.request.url).netloc != "spark.test":
            request.abort()
            return
        path = urlsplit(request.request.url).path
        if path == "/api/media":
            request.fulfill(
                content_type="image/svg+xml",
                body=(
                    '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240">'
                    '<rect width="240" height="240" fill="black"/></svg>'
                ),
            )
            return
        if path == "/" or path.startswith("/static/"):
            file = static / ("index.html" if path == "/" else path.removeprefix("/static/"))
            if not file.is_file():
                request.fulfill(status=404)
                return
            request.fulfill(
                body=file.read_bytes(),
                content_type=mimetypes.guess_type(file.name)[0] or "text/plain",
            )
            return
        if request.request.method == "POST":
            state["posts"].append((path, request.request.post_data_json))
        if path == "/api/status":
            result = {
                "recent_jobs": [{"job_id": "test-session", "kind": "login", "state": "running"}]
            }
        elif path == "/api/login/test-session":
            result = session
        elif path.endswith("/qr/refresh"):
            session["qr_version"] += 1
            result = {"message": "二维码已刷新"}
        else:
            result = {}
        request.fulfill(json=result)

    page.route("**/*", route)
    page.goto("http://spark.test/", wait_until="domcontentloaded")
    expect(page.locator("#account-add-dialog")).to_be_visible()
    expect(page.locator("#login-progress")).to_be_visible()
    return state


@pytest.mark.parametrize(
    "viewport", [{"width": 1280, "height": 720}, {"width": 390, "height": 480}]
)
def test_manual_refresh_is_visible_without_scrolling_and_updates_qr(page, viewport):
    page.set_viewport_size(viewport)
    state = open_panel(page)
    expect(page.locator("#login-qr")).to_be_visible()
    expect(page.locator("#login-qr-refresh")).to_be_in_viewport(ratio=1, timeout=2000)
    page.locator("#login-qr-refresh").click()
    expect(page.locator("#login-qr")).to_have_attribute("src", re.compile(r"[?&]v=2(?:&|$)"))
    assert state["posts"] == [("/api/login/test-session/qr/refresh", {})]
    assert state["errors"] == []


def test_refresh_is_hidden_during_sms_verification(page):
    state = open_panel(page, "sms_required")
    expect(page.locator("#login-sms")).to_be_visible()
    expect(page.locator("#login-qr-refresh")).to_be_hidden()
    assert state["posts"] == []
