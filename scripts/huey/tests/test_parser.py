import sys
from pathlib import Path

sys.path.insert(0, "/app/scripts/huey")

from parser import parse_request


def test_by_format():
    result = parse_request(
        "Harry Potter and the Order of the Phoenix by J.K. Rowling"
    )
    assert result["title"] == "Harry Potter and the Order of the Phoenix"
    assert result["author"] == "J.K. Rowling"


def test_title_only():
    result = parse_request(
        "Harry Potter and the Order of the Phoenix"
    )
    assert result["title"] == "Harry Potter and the Order of the Phoenix"
    assert result["author"] is None
