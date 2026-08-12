def handle(request):
    return {
        "handler": "ebooks",
        "status": "queued",
        "message": "ebook request ready for processing",
    }
