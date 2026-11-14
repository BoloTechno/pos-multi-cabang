from django.conf import settings
from .services import locations_for


def app_context(request):
    return {"currency": settings.CURRENCY, "managed_locations": locations_for(request.user, manage=True)}
