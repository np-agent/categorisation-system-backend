from typing import Any, Dict, Optional

from supertokens_python import init, InputAppInfo, SupertokensConfig
from supertokens_python.recipe import emailpassword, session, dashboard
from supertokens_python.recipe.emailpassword.types import EmailDeliveryInterface
from supertokens_python.ingredients.emaildelivery.types import EmailDeliveryConfig

from config.settings import settings


class ResendEmailDelivery(EmailDeliveryInterface):  # type: ignore[misc]
    """
    Deliver SuperTokens emailpassword emails (password reset / invite) via Resend.
    Falls back gracefully if RESEND_API_KEY is not set.
    """

    async def send_email(self, template_vars: Any, user_context: Dict[str, Any]) -> None:
        from services.email import send_password_reset_email

        reset_link: Optional[str] = getattr(template_vars, "password_reset_link", None)
        to_email: str = getattr(template_vars, "email", "")

        if reset_link and to_email:
            send_password_reset_email(to_email=to_email, reset_link=reset_link)


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
        ),
        dashboard.init(),
    ],
    mode="asgi",
)
