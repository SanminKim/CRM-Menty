from django.urls import include, path

# Служебного раздела Django (/admin/) нет: учётными записями управляют в настройках самой CRM
urlpatterns = [
    path("", include("backend.urls")),
]
