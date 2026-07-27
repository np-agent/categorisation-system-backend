from supertokens_python import init, InputAppInfo, SupertokensConfig
from supertokens_python.recipe import emailpassword, session, dashboard

from config.settings import settings

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
        emailpassword.init(),
        dashboard.init(),
    ],
    mode="asgi",
)
