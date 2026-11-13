from katnip.utils import safe_slug, strip_url_query


def test_strip_url_query() -> None:
    assert strip_url_query("https://example.com/a?x=1#part") == "https://example.com/a"


def test_safe_slug() -> None:
    assert safe_slug("Hello / World") == "Hello-World"
