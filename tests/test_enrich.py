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


def test_eligibility_excerpt_centers_on_eligibility_section():
    html = "<p>" + "preamble " * 500 + "Eligibility: Teams of up to 5 individuals." + " tail" * 200 + "</p>"
    excerpt = _eligibility_excerpt(html)
    assert "Teams of up to 5" in excerpt
    assert len(excerpt) <= 2_500
