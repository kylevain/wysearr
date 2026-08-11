import re

def parse_request(text):
    text = " ".join(text.strip().split())

    if " by " in text.lower():
        parts = re.split(r"\s+by\s+", text, maxsplit=1, flags=re.I)
        return {
            "title": parts[0].strip(),
            "author": parts[1].strip()
        }

    return {
        "title": text,
        "author": None
    }
