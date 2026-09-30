from decimal import Decimal, InvalidOperation

from django import template
from django.conf import settings
from django.utils.html import format_html

register = template.Library()


@register.filter
def money(value):
    """1234567.5 → «1 234 568 ₽»."""
    if value in (None, ""):
        return "—"
    try:
        value = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return value
    s = f"{value:,.0f}".replace(",", " ")
    return f"{s} {settings.CRM_CURRENCY}"


@register.filter
def initials(name):
    parts = [p for p in str(name).split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


@register.filter
def mask_name(name):
    """Для отчёта партнёра: «Иванов Пётр» → «Пётр И.» (без лишних персональных данных)."""
    parts = [p for p in str(name).split() if p]
    if len(parts) >= 2:
        return f"{parts[1]} {parts[0][0]}."
    return parts[0] if parts else ""


@register.filter
def user_name(user):
    if not user:
        return "—"
    return user.get_full_name() or user.username


@register.simple_tag
def stage_badge(student):
    return format_html(
        '<span class="badge badge-{}">{}</span>', student.stage_group, student.get_stage_display()
    )


@register.filter
def plural(n, forms):
    """{{ n|plural:"день,дня,дней" }}"""
    one, few, many = forms.split(",")
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


@register.filter
def get_item(d, key):
    return d.get(key) if hasattr(d, "get") else None
