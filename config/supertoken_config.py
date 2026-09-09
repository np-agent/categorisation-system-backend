import logging
from typing import Any, Dict, List, Optional, Union

from supertokens_python import init, InputAppInfo, SupertokensConfig
from supertokens_python.recipe import emailpassword, session, dashboard
from supertokens_python.recipe.emailpassword.interfaces import (
    APIInterface,
    APIOptions,
    PasswordResetPostOkResult,
    SignInPostOkResult,
)
from supertokens_python.recipe.emailpassword.types import (
    EmailDeliveryInterface,
    FormField,
)
from supertokens_python.recipe.session import SessionContainer
from supertokens_python.ingredients.emaildelivery.types import EmailDeliveryConfig
from supertokens_python.types import GeneralErrorResponse

from config.settings import settings

logger = logging.getLogger(__name__)

BLOCKED_ACCOUNT = "account"
BLOCKED_ORG = "org"

SIGN_IN_BLOCKED_MESSAGE = {
    BLOCKED_ACCOUNT: (
        "Your account has been deactivated. Please contact your administrator."
    ),
    BLOCKED_ORG: (
        "Your organisation has been deactivated. Please contact your administrator."
    ),
}

# The reset token is consumed before we can tell whose it was, so by this point
# the new password really is saved. Saying otherwise would send them back to
# request another reset that also appears to fail.
PASSWORD_RESET_BLOCKED_MESSAGE = {
    BLOCKED_ACCOUNT: (
        "Your password was updated, but your account has been deactivated, "
        "so you cannot sign in. Please contact your administrator."
    ),
    BLOCKED_ORG: (
        "Your password was updated, but your organisation has been deactivated, "
        "so you cannot sign in. Please contact your administrator."
    ),
}


class ResendEmailDelivery(EmailDeliveryInterface):  # type: ignore[misc]
    """
    Deliver SuperTokens emailpassword emails (password reset / invite) via Resend.
    Falls back gracefully if RESEND_API_KEY is not set.
    """

    async def send_email(self, template_vars: Any, user_context: Dict[str, Any]) -> None:
        from services.email import send_password_reset_email
        import logging

        logger = logging.getLogger(__name__)

        reset_link: Optional[str] = getattr(template_vars, "password_reset_link", None)
        user = getattr(template_vars, "user", None)
        to_email: str = getattr(user, "email", "") if user is not None else ""

        if not reset_link or not to_email:
            logger.warning(
                "Password reset email skipped — missing link or email "
                "(link=%s, email=%s)",
                bool(reset_link),
                to_email or None,
            )
            return

        ok = send_password_reset_email(to_email=to_email, reset_link=reset_link)
        if not ok:
            logger.error("Password reset email failed to send to %s", to_email)


async def _blocked_kind(supertokens_user_id: str) -> Optional[str]:
    """
    Why this account is barred (BLOCKED_ACCOUNT / BLOCKED_ORG), or None if it
    is not.

    On any lookup failure we allow the attempt through: the request-time gate in
    api/deps.py is the real enforcement point, so a database hiccup here should
    not lock everybody out of the login screen.
    """
    try:
        from database.session import get_database

        db = await get_database()
        user = await db["users"].find_one(
            {"supertokens_user_id": supertokens_user_id},
            {"is_active": 1, "organization_id": 1, "deactivated_by_org": 1},
        )
        # No profile yet — it gets created on the first /api/v1/me call.
        if not user:
            return None
        if not user.get("is_active", True):
            # Name the real cause: the org cascade switches the user flag off
            # too, and blaming their account would send them chasing the wrong
            # thing.
            return BLOCKED_ORG if user.get("deactivated_by_org") else BLOCKED_ACCOUNT

        org_id = user.get("organization_id")
        if org_id is None:
            return None
        org = await db["organizations"].find_one({"_id": org_id}, {"is_active": 1})
        if org is None or not org.get("is_active", True):
            return BLOCKED_ORG
        return None
    except Exception:
        logger.exception("Could not check access for %s", supertokens_user_id)
        return None


def _override_apis(original: APIInterface) -> APIInterface:
    """
    Refuse sign-in, and explain a completed password reset, for deactivated
    accounts and organisations.

    SuperTokens issues the session before any of our own code runs, so without
    this the user would briefly hold a valid session and only be ejected once
    the frontend saw a 403. Here the freshly created session is revoked and a
    plain message is returned instead.
    """
    original_sign_in_post = original.sign_in_post
    original_password_reset_post = original.password_reset_post

    async def sign_in_post(
        form_fields: List[FormField],
        tenant_id: str,
        session: Union[SessionContainer, None],
        should_try_linking_with_session_user: Union[bool, None],
        api_options: APIOptions,
        user_context: Dict[str, Any],
    ):
        result = await original_sign_in_post(
            form_fields,
            tenant_id,
            session,
            should_try_linking_with_session_user,
            api_options,
            user_context,
        )
        if not isinstance(result, SignInPostOkResult):
            return result

        kind = await _blocked_kind(result.user.id)
        if kind is None:
            return result

        try:
            await result.session.revoke_session()
        except Exception:
            logger.exception("Could not revoke session for blocked sign-in")
        return GeneralErrorResponse(message=SIGN_IN_BLOCKED_MESSAGE[kind])

    async def password_reset_post(
        form_fields: List[FormField],
        token: str,
        tenant_id: str,
        api_options: APIOptions,
        user_context: Dict[str, Any],
    ):
        """
        Tell an invitee why their new password will not get them in, instead of
        reporting plain success and letting them discover it at the login screen.
        """
        result = await original_password_reset_post(
            form_fields, token, tenant_id, api_options, user_context
        )
        if not isinstance(result, PasswordResetPostOkResult):
            return result

        kind = await _blocked_kind(result.user.id)
        if kind is None:
            return result
        return GeneralErrorResponse(message=PASSWORD_RESET_BLOCKED_MESSAGE[kind])

    original.sign_in_post = sign_in_post
    original.password_reset_post = password_reset_post
    return original


init(
    app_info=InputAppInfo(
        app_name="Categorisation System",
        api_domain=settings.API_DOMAIN,
        website_domain=settings.WEBSITE_DOMAIN,
        api_base_path="/auth",
        website_base_path="/",
    ),
    supertokens_config=SupertokensConfig(
        connection_uri=settings.SUPERTOKENS_CONNECTION_URI,
        api_key=settings.SUPERTOKENS_API_KEY,
    ),
    framework="fastapi",
    recipe_list=[
        session.init(),
        emailpassword.init(
            email_delivery=EmailDeliveryConfig(service=ResendEmailDelivery()),
            override=emailpassword.InputOverrideConfig(apis=_override_apis),
        ),
        dashboard.init(),
    ],
    mode="asgi",
)
