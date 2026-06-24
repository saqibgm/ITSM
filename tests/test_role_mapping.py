"""IAM role -> ITSM role mapping. Regression guard for the live bug where
operator/user/bot are Keycloak *client* roles (capitalized 'Operator'/'User'/
'Bot'), so realm-only + case-sensitive matching wrongly defaulted operators to
end_user instead of agent."""
import pytest

from app.auth.dependencies import _best_platform_role, _best_tenant_role


@pytest.mark.parametrize("roles,expected", [
    (["Operator"], "agent"),              # client role (capitalized) — the bug
    (["operator"], "agent"),              # realm/lowercase still works
    (["User"], "end_user"),
    (["Bot"], "service_account"),
    (["Admin"], "admin"),
    (["org_admin"], "admin"),             # realm role unchanged
    (["super_admin"], "admin"),
    (["Operator", "User"], "agent"),      # highest-privilege wins
    (["Admin", "Operator"], "admin"),
    (["default-roles-99-iam", "app_owner"], "end_user"),  # unmapped -> default
    ([], "end_user"),
])
def test_best_tenant_role(roles, expected):
    assert _best_tenant_role(roles) == expected


@pytest.mark.parametrize("roles,expected", [
    (["super_admin"], "platform_admin"),
    (["org_admin"], "platform_admin"),
    (["Operator"], "platform_support"),   # client role mapped case-insensitively
    (["operator"], "platform_support"),
    (["app_developer"], "platform_support"),
    (["super_admin", "Operator"], "platform_admin"),  # highest wins
    (["User"], None),
    ([], None),
])
def test_best_platform_role(roles, expected):
    assert _best_platform_role(roles) == expected
