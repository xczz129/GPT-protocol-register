import web_app


def test_auth_routes_are_registered_and_reexported_from_web_app():
    paths = web_app.app.openapi()["paths"]
    expected = {
        "/login",
        "/api/auth/status",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/change-password",
        "/api/auth/revoke-sessions",
    }

    assert expected <= set(paths)
    assert web_app.auth_login.__module__ == "web.routes.auth"
    assert web_app.LoginRequest.__module__ == "web.routes.auth"
