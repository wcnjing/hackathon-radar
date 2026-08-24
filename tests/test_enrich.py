from hackathon_radar.enrich import _eligibility_excerpt, _page_text


def test_page_text_strips_markup():
    html = """
    <html><head><style>.x { color: red }</style>
    <script>var tracking = "junk";</script></head>
    <body><h1>Big   Hackathon</h1><p>Build &amp; ship an AI agent.</p></body></html>
    """
    text = _page_text(html, 1000)
    assert "Big Hackathon" in text
    assert "Build & ship an AI agent." in text
    assert "tracking" not in text
    assert "color" not in text


def test_page_text_respects_limit():
    assert len(_page_text("<p>" + "word " * 10_000 + "</p>", 500)) == 500


def test_page_text_preserves_links():
    """Regression: stripping tags destroyed hrefs, so extracted events could
    never carry registration links from HTML content."""
    html = '<p>Join us! <a href="https://unstop.com/hack123?utm=x">Register here</a></p>'
    text = _page_text(html, 1000)
    assert "Register here (https://unstop.com/hack123?utm=x)" in text


def test_page_text_drops_junk_links():
    html = '<a href="javascript:void(0)">Click</a> <a href="mailto:a@b.c">Mail</a>'
    text = _page_text(html, 1000)
    assert "Click" in text and "Mail" in text
    assert "javascript" not in text and "mailto" not in text


def test_eligibility_excerpt_centers_on_eligibility_section():
    html = (
        "<p>"
        + "preamble " * 500
        + "Eligibility: Teams of up to 5 individuals."
        + " tail" * 200
        + "</p>"
    )
    excerpt = _eligibility_excerpt(html)
    assert "Teams of up to 5" in excerpt
    assert len(excerpt) <= 2_500
