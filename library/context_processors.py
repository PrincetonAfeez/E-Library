from .tenancy import get_current_organization


def current_organization(request):
    return {"current_organization": get_current_organization(request)}
