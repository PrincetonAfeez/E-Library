"""Root URL configuration for the E-Library project."""
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from library.ratelimit import rate_limit

urlpatterns = [
    path("admin/", admin.site.urls),
    path(
        "accounts/login/",
        # Throttle credential submissions per IP to blunt brute-forcing.
        rate_limit(scope="login", limit=10, window=300)(
            auth_views.LoginView.as_view(template_name="registration/login.html")
        ),
        name="login",
    ),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    # Self-service password recovery. Tokens are single-use and time-limited by
    # Django (PASSWORD_RESET_TIMEOUT); the request step is rate-limited per IP.
    path(
        "accounts/password_reset/",
        rate_limit(scope="password_reset", limit=5, window=900)(
            auth_views.PasswordResetView.as_view()
        ),
        name="password_reset",
    ),
    path(
        "accounts/password_reset/done/",
        auth_views.PasswordResetDoneView.as_view(),
        name="password_reset_done",
    ),
    path(
        "accounts/reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path(
        "accounts/reset/done/",
        auth_views.PasswordResetCompleteView.as_view(),
        name="password_reset_complete",
    ),
    path("i18n/", include("django.conf.urls.i18n")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("", include("library.urls")),
]
