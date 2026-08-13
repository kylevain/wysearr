from .common import handle_book


def handle(request, services=None):
    return handle_book(request, services)
