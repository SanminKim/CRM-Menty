from django.conf import settings

from .permissions import get_partner, is_admin, is_mentor


def crm_globals(request):
    user = request.user
    return {
        "CURRENCY": settings.CRM_CURRENCY,
        "STUCK_DAYS": settings.CRM_STUCK_DAYS,
        "user_is_admin": is_admin(user),
        "user_is_mentor": is_mentor(user),
        "user_partner": get_partner(user),
    }
