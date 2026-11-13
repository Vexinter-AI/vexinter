from fastapi.testclient import TestClient

from services.app import make_app
from vexinter.core.store import InMemoryEventStore
from vexinter.demo import CrmSystem


def _consola(**kw):
    return make_app(store=InMemoryEventStore(), adapters=[CrmSystem()],
                    dev_mode=False, admin_token="tok", **kw)


class _Reloj:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_login_is_locked_after_several_failed_attempts():
    from services.console import LoginLimiter
    app = _consola()
    reloj = _Reloj()
    app.state.login_limiter = LoginLimiter(max_fails=3, lock_s=60, clock=reloj)
    c = TestClient(app)
    for _ in range(3):
        r = c.post("/console/login", data={"token": "malo"},
                   follow_redirects=False)
        assert r.status_code == 200
    r = c.post("/console/login", data={"token": "tok"},
               follow_redirects=False)
    assert r.status_code == 429            # locked even with the right one
    reloj.t += 61
    r = c.post("/console/login", data={"token": "tok"},
               follow_redirects=False)
    assert r.status_code == 303


def test_a_success_resets_failed_attempts():
    from services.console import LoginLimiter
    app = _consola()
    app.state.login_limiter = LoginLimiter(max_fails=3, lock_s=60,
                                           clock=_Reloj())
    c = TestClient(app)
    for _ in range(2):
        c.post("/console/login", data={"token": "malo"},
               follow_redirects=False)
    assert c.post("/console/login", data={"token": "tok"},
                  follow_redirects=False).status_code == 303
    for _ in range(2):
        r = c.post("/console/login", data={"token": "malo"},
                   follow_redirects=False)
        assert r.status_code == 200        # the counter started from zero


def test_old_failures_do_not_count_for_the_lockout():
    from services.console import LoginLimiter
    reloj = _Reloj()
    lim = LoginLimiter(max_fails=3, window_s=100, lock_s=60, clock=reloj)
    lim.failure("1.2.3.4")
    lim.failure("1.2.3.4")
    reloj.t += 101
    lim.failure("1.2.3.4")
    assert lim.blocked("1.2.3.4") == 0
    lim.failure("1.2.3.4")
    lim.failure("1.2.3.4")
    assert lim.blocked("1.2.3.4") > 0
    assert lim.blocked("5.6.7.8") == 0


def test_the_session_cookie_is_secure_by_default(monkeypatch):
    monkeypatch.delenv("VEXINTER_COOKIE_SECURE", raising=False)
    r = TestClient(_consola()).post("/console/login", data={"token": "tok"},
                                    follow_redirects=False)
    ck = r.headers["set-cookie"].lower()
    assert "secure" in ck and "httponly" in ck and "samesite=strict" in ck
    monkeypatch.setenv("VEXINTER_COOKIE_SECURE", "0")
    r2 = TestClient(_consola()).post("/console/login", data={"token": "tok"},
                                     follow_redirects=False)
    assert "secure" not in r2.headers["set-cookie"].lower()


def test_console_messages_travel_url_encoded():
    app = make_app(store=InMemoryEventStore(), adapters=[CrmSystem()],
                  dev_mode=True)
    c = TestClient(app)
    datos = {"agent_id": "x&y#z", "allowed_tools": "",
            "allowed_recipients": ""}
    c.post("/console/agents/enroll", data=datos, follow_redirects=False)
    r = c.post("/console/agents/enroll", data=datos,
              follow_redirects=False)                      # repeated: error
    loc = r.headers["location"]
    assert "%26" in loc and "%23" in loc and "&amp;" not in loc
    pagina = c.get(loc).text
    assert "x&amp;y#z" in pagina and "&amp;amp;" not in pagina


def test_the_compiled_ui_is_served_at_ui(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<html>hola ui</html>")
    monkeypatch.setenv("VEXINTER_UI_DIST", str(tmp_path))
    app = make_app(store=InMemoryEventStore(), adapters=[CrmSystem()],
                   dev_mode=True)
    r = TestClient(app).get("/app/")
    assert r.status_code == 200 and "hola ui" in r.text


def test_the_ui_resolves_client_routes_on_reload(tmp_path,
                                                            monkeypatch):
    (tmp_path / "index.html").write_text("<html>hola ui</html>")
    monkeypatch.setenv("VEXINTER_UI_DIST", str(tmp_path))
    app = make_app(store=InMemoryEventStore(), adapters=[CrmSystem()],
                   dev_mode=True)
    r = TestClient(app).get("/app/tasks/task_123")
    assert r.status_code == 200 and "hola ui" in r.text


def test_a_missing_ui_file_is_still_404(tmp_path,
                                                                  monkeypatch):
    (tmp_path / "index.html").write_text("<html>hola ui</html>")
    monkeypatch.setenv("VEXINTER_UI_DIST", str(tmp_path))
    app = make_app(store=InMemoryEventStore(), adapters=[CrmSystem()],
                   dev_mode=True)
    assert TestClient(app).get("/app/assets/no-existe.js").status_code == 404


def test_the_spa_exchanges_token_for_httponly_cookie():
    c = TestClient(_consola())
    r = c.post("/v1/auth/login", json={"token": "malo"})
    assert r.status_code == 403

    r = c.post("/v1/auth/login", json={"token": "tok"})
    assert r.status_code == 204
    cookie = r.headers["set-cookie"]
    assert "vex_spa=" in cookie and "HttpOnly" in cookie
    assert "tok" not in cookie.split(";")[0]  # the cookie does not carry the token

    assert c.get("/v1/auth/session").json()["authenticated"] is True
    # The cookie authenticates admin endpoints without a header
    assert c.get("/v1/metrics").status_code == 200

    c.delete("/v1/auth/logout")
    assert c.get("/v1/auth/session").json()["authenticated"] is False
    assert c.get("/v1/metrics").status_code == 403


def test_the_session_cookie_cannot_be_forged():
    c = TestClient(_consola())
    # Cookie with valid format but invalid signature
    c.cookies.set("vex_spa", "admin.9999999999.firma_falsa")
    assert c.get("/v1/auth/session").json()["authenticated"] is False
    assert c.get("/v1/metrics").status_code == 403


def test_the_header_still_works_for_non_browser_clients():
    c = TestClient(_consola())
    assert c.get("/v1/metrics",
                 headers={"X-Vex-Admin": "tok"}).status_code == 200
