"""Роли пользователей.

- Администратор: суперпользователь или член группы «Администраторы» — видит всё.
- Ментор: член группы «Менторы» — видит только закреплённых за ним студентов.
- Партнёр: пользователь, привязанный к записи Partner — видит только отчёт по своему трафику.
"""
from functools import wraps

from django.core.exceptions import PermissionDenied

from .models import Student

ADMIN_GROUP = "Администраторы"
MENTOR_GROUP = "Менторы"


def is_admin(user):
    return user.is_authenticated and (
        user.is_superuser or user.groups.filter(name=ADMIN_GROUP).exists()
    )


def is_mentor(user):
    return user.is_authenticated and user.groups.filter(name=MENTOR_GROUP).exists()


def get_partner(user):
    return getattr(user, "partner", None) if user.is_authenticated else None


def is_staff_member(user):
    """Сотрудник школы: админ или ментор."""
    return is_admin(user) or is_mentor(user)


def visible_students(user):
    qs = Student.objects.select_related("cohort", "mentor", "partner")
    if is_admin(user):
        return qs
    if is_mentor(user):
        return qs.filter(mentor=user)
    return qs.none()


def staff_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login
            return redirect_to_login(request.get_full_path())
        if not is_staff_member(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login
            return redirect_to_login(request.get_full_path())
        if not is_admin(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper
