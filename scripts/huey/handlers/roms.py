from .common import handle_direct


def handle(request, services=None):
    return handle_direct(request, services)
