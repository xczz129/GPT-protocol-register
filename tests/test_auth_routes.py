import web_app


def test_auth_routes_are_registered_and_reexported_from_web_app():
    paths = web_app.app.openapi()["paths"]
    expected = {
        "/login",
        "/redeem",
        "/cdk-query",
        "/api/auth/status",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/change-password",
        "/api/auth/revoke-sessions",
        "/api/redeem",
        "/api/redeem/batch",
        "/api/cdk-query",
        "/api/redeem/session",
        "/api/redeem/access-token",
        "/api/redeem/account.json",
        "/api/redeem/messages",
        "/api/redeem/message",
        "/api/redeem/logout",
    }

    assert expected <= set(paths)
    assert web_app.auth_login.__module__ == "web.routes.auth"
    assert web_app.redeem_batch_exchange.__module__ == "web.routes.auth"
    assert web_app.LoginRequest.__module__ == "web.routes.auth"
