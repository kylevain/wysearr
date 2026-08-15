from .common import handle_audiobook


def handle(request, services=None):
    return handle_audiobook(request, services)
