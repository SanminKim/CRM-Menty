from django.contrib.auth import views as auth_views
from django.urls import path

from . import api, leads, views

urlpatterns = [
    path("", views.app, name="home"),
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("password/", views.PasswordChangeView.as_view(), name="password"),
    path("password/done/", views.password_done, name="password_done"),
    path("backup/import/", views.backup_import, name="backup_import"),
    path("api/me/", api.me),
    path("api/sync/", api.sync),
    path("api/db/<str:collection>/<str:doc_id>/", api.doc),
    path("api/profiles/", api.profiles),
    path("api/accounts/", api.accounts),
    path("api/accounts/<int:pk>/", api.account),
    path("api/leads/", leads.api_lead, name="api_lead"),
]
