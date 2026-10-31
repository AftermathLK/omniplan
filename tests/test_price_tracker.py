"""Price extraction, URL safety, deal feeds and product lookups (no network)."""

from __future__ import annotations

import json

import pytest

from taskdragon import price_tracker
from taskdragon.price_tracker import (
    FetchError,
    GuardedFetcher,
    LookupLimit,
    ProductLookup,
    PublicResolver,
    UnsafeUrl,
    check_selector,
    check_url,
    extract_offer,
    filter_deals,
    first_dollar_price,
    matches_query,
    normalize_availability,
    parse_price,
    parse_reddit_deals,
    parse_rss_deals,
    parse_upcitemdb,
    query_tokens,
    reddit_url,
    retailer_name,
    search_links,
    slickdeals_url,
)


# ---------------------------------------------------------------------------
# URL safety
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "https://www.bestbuy.com/site/lg-27gp850/6453.p?skuId=6453",
        "http://example.com/product",
        "https://shop.example.co.uk:443/item",
        "https://93.184.216.34/item",
    ],
)
def test_public_urls_are_allowed(url: str) -> None:
    assert check_url(f"  {url} ") == url


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "http://localhost:8123/api",
        "http://127.0.0.1/",
        "http://10.0.0.8/",
        "http://192.168.1.20/admin",
        "http://172.16.4.4/",
        "http://100.64.0.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://printer.local/",
        "http://homeassistant.home.arpa/",
        "http://router/",
        "http://user:secret@example.com/",
        "http://example.com:8123/",
        "https://example.com/has space",
        "",
    ],
)
def test_private_and_odd_urls_are_refused(url: str) -> None:
    with pytest.raises(UnsafeUrl):
        check_url(url)


def test_selector_validation() -> None:
    assert check_selector("  .price-now ") == ".price-now"
    assert check_selector("#itemFinalPrice@data-final-price") == "#itemFinalPrice@data-final-price"
    assert check_selector("[data-test='a@b']") == "[data-test='a@b']"
    assert check_selector("") is None and check_selector(None) is None
    for bad in ("div[", "span@not an attr", "x" * 400):
        with pytest.raises(ValueError):
            check_selector(bad)


async def test_resolver_drops_private_answers(monkeypatch) -> None:
    resolver = PublicResolver()

    async def fake_resolve(host, port=0, family=0):
        return [{"hostname": host, "host": "10.1.2.3", "port": port, "family": 2, "proto": 0, "flags": 0}, {"hostname": host, "host": "93.184.216.34", "port": port, "family": 2, "proto": 0, "flags": 0}]

    monkeypatch.setattr(resolver._inner, "resolve", fake_resolve)
    answers = await resolver.resolve("shop.example", 443)
    assert [a["host"] for a in answers] == ["93.184.216.34"]

    async def rebinding(host, port=0, family=0):
        return [{"hostname": host, "host": "127.0.0.1", "port": port, "family": 2, "proto": 0, "flags": 0}]

    monkeypatch.setattr(resolver._inner, "resolve", rebinding)
    with pytest.raises(OSError):
        await resolver.resolve("evil.example", 443)
    await resolver.close()


class _Content:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def iter_chunked(self, size: int):
        for start in range(0, len(self.body), size):
            yield self.body[start : start + size]


class _Response:
    def __init__(self, url: str, status: int, body: bytes = b"", headers: dict | None = None) -> None:
        self.url = url
        self.status = status
        self.headers = headers or {}
        self.content = _Content(body)
        self.charset = "utf-8"
        self.content_type = "text/html"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Answers from a {url: response} map and records what was asked."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[str] = []
        self.closed = False

    def get(self, url, headers=None, allow_redirects=True):
        assert allow_redirects is False
        self.calls.append(url)
        return self.routes[url]

    async def close(self) -> None:
        self.closed = True


async def test_fetcher_rechecks_every_redirect() -> None:
    session = _Session(
        {
            "https://shop.example/p": _Response("https://shop.example/p", 302, headers={"Location": "/p2"}),
            "https://shop.example/p2": _Response("https://shop.example/p2", 200, b"<html>ok</html>"),
            "https://evil.example/x": _Response("https://evil.example/x", 301, headers={"Location": "http://192.168.1.1/admin"}),
        }
    )
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    fetcher = GuardedFetcher(session_factory=lambda: session, sleep=sleep, monotonic=lambda: 100.0)
    page = await fetcher.get("https://shop.example/p", headers={}, max_bytes=1000)
    assert page.body == b"<html>ok</html>" and session.calls == ["https://shop.example/p", "https://shop.example/p2"]
    assert sleeps, "the second request to the same store waits its turn"
    with pytest.raises(FetchError, match="can't be tracked"):
        await fetcher.get("https://evil.example/x", headers={}, max_bytes=1000)
    with pytest.raises(FetchError):
        await fetcher.get("http://127.0.0.1/", headers={}, max_bytes=1000)
    assert "http://192.168.1.1/admin" not in session.calls and "http://127.0.0.1/" not in session.calls
    await fetcher.close()
    assert session.closed


async def test_fetcher_size_cap_and_status() -> None:
    session = _Session(
        {
            "https://big.example/p": _Response("https://big.example/p", 200, b"x" * 5000),
            "https://blocked.example/p": _Response("https://blocked.example/p", 403),
        }
    )
    fetcher = GuardedFetcher(session_factory=lambda: session)
    page = await fetcher.get("https://big.example/p", headers={}, max_bytes=1000, truncate=True)
    assert len(page.body) == 1000 and page.truncated
    with pytest.raises(FetchError, match="larger than allowed"):
        await fetcher.get("https://big.example/p", headers={}, max_bytes=1000)
    with pytest.raises(FetchError) as caught:
        await fetcher.get("https://blocked.example/p", headers={}, max_bytes=1000)
    assert caught.value.status == 403 and caught.value.blocked


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$1,299.99", 1299.99),
        ("249", 249.0),
        (279.0, 279.0),
        ("USD 34.5", 34.5),
        ("1.299,99 €", 1299.99),
        ("12,99 €", 12.99),
        ("1.299.000", 1299000.0),
        ("Now $49.00 (was $79)", 49.0),
        ("free", None),
        ("0", None),
        (True, None),
        ("-5", 5.0),
    ],
)
def test_parse_price(raw, expected) -> None:
    assert parse_price(raw) == expected


def test_availability_words() -> None:
    assert normalize_availability("https://schema.org/InStock") is True
    assert normalize_availability("http://schema.org/OutOfStock") is False
    assert normalize_availability("Currently unavailable.") is False
    assert normalize_availability("In Stock.") is True
    assert normalize_availability("PreOrder") is True
    assert normalize_availability("") is None and normalize_availability("maybe") is None


def _page(head: str = "", body: str = "") -> bytes:
    return f"<!doctype html><html><head><title>Fallback title</title>{head}</head><body>{body}</body></html>".encode()


def test_jsonld_product_in_graph_prefers_buyable_offer() -> None:
    data = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebPage", "name": "Page"},
            {
                "@type": ["Product"],
                "name": "LG UltraGear 27\" 4K",
                "image": [{"@type": "ImageObject", "url": "/img/lg.jpg"}],
                "offers": [
                    {"@type": "Offer", "price": "199.99", "priceCurrency": "USD", "availability": "https://schema.org/OutOfStock"},
                    {"@type": "Offer", "price": "249.99", "priceCurrency": "USD", "availability": "https://schema.org/InStock"},
                ],
            },
        ],
    }
    found = extract_offer(_page(f'<script type="application/ld+json">{json.dumps(data)}</script>'), "https://shop.example/p/1")
    assert (found.price, found.currency, found.in_stock, found.method) == (249.99, "USD", True, "jsonld")
    assert found.title == 'LG UltraGear 27" 4K' and found.image == "https://shop.example/img/lg.jpg"


def test_jsonld_aggregate_offer_and_trailing_comma() -> None:
    script = '<script type="application/ld+json">{"@type":"Product","name":"Router","offers":{"@type":"AggregateOffer","lowPrice":"89.00","highPrice":"129.00","priceCurrency":"USD",},}</script>'
    found = extract_offer(_page(script), "https://shop.example/router")
    assert found.price == 89.0 and found.method == "jsonld"


def test_meta_tags_and_itemprop() -> None:
    head = '<meta property="og:title" content="Rain boots"><meta property="product:price:amount" content="34.95"><meta property="product:price:currency" content="usd"><meta property="og:availability" content="instock">'
    found = extract_offer(_page(head), "https://kids.example/boots")
    assert (found.price, found.currency, found.in_stock, found.method, found.title) == (34.95, "USD", True, "meta", "Rain boots")
    found = extract_offer(_page(body='<div><span itemprop="price" content="19.50">$19.50</span></div>'), "https://kids.example/hat")
    assert found.price == 19.5 and found.method == "meta" and found.title == "Fallback title"


def test_retailer_selectors() -> None:
    amazon = _page(body='<div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">$279.00</span></span></div><div id="availability">In Stock</div>')
    found = extract_offer(amazon, "https://www.amazon.com/dp/B0TEST")
    assert (found.price, found.method, found.in_stock) == (279.0, "retailer", True)
    depot = _page(body='<div class="price-format__main-price"><span>$</span><span>249</span><span>97</span></div>')
    assert extract_offer(depot, "https://www.homedepot.com/p/123").price == 249.97
    menards = _page(body='<span id="itemFinalPrice" data-final-price="12.99">see cart</span>')
    assert extract_offer(menards, "https://www.menards.com/main/p-1.htm").price == 12.99


def test_custom_selector_overrides_and_reads_attributes() -> None:
    script = '<script type="application/ld+json">{"@type":"Product","offers":{"price":"500"}}</script>'
    body = '<div class="sale" data-amount="425.00">Sale!</div>'
    found = extract_offer(_page(script, body), "https://niche.example/p", custom_selector=".sale@data-amount")
    assert (found.price, found.method) == (425.0, "custom")
    found = extract_offer(_page(script, body), "https://niche.example/p", custom_selector=".missing")
    assert (found.price, found.method) == (500.0, "jsonld")


def test_no_price_found() -> None:
    found = extract_offer(_page(body="<p>Call for pricing</p>"), "https://shop.example/x")
    assert found.price is None and found.method is None


def test_retailer_names() -> None:
    assert retailer_name("https://www.bestbuy.com/site/x") == "Best Buy"
    assert retailer_name("https://smile.amazon.co.uk/dp/x") == "Amazon"
    assert retailer_name("https://www.bhphotovideo.com/c/x") == "B&H"
    assert retailer_name("https://www.wayfair.com/x") == "Wayfair"
    assert retailer_name("https://shop.tinybrand.co.uk/x") == "Tinybrand"


# ---------------------------------------------------------------------------
# Deal radar
# ---------------------------------------------------------------------------
RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Slickdeals</title>
<item><title>LG 27GP950 27" 4K Monitor $449.99</title><link>https://slickdeals.net/f/1</link><pubDate>Tue, 27 Oct 2026 14:00:00 +0000</pubDate></item>
<item><title>Samsung 32" 4K Smart Monitor M7 - $279</title><link>https://slickdeals.net/f/2</link><pubDate>Mon, 26 Oct 2026 10:00:00 +0000</pubDate></item>
<item><title>Cordless drill kit</title><link>https://slickdeals.net/f/3</link></item>
<item><title>Bad link</title><link>javascript:alert(1)</link></item>
</channel></rss>"""


def test_rss_deals_parse_and_filter() -> None:
    deals = parse_rss_deals(RSS)
    assert [d.url for d in deals] == ["https://slickdeals.net/f/1", "https://slickdeals.net/f/2", "https://slickdeals.net/f/3"]
    assert deals[0].price == 449.99 and deals[0].posted_at == "2026-10-27T14:00:00Z" and deals[1].price == 279.0
    matched = filter_deals(deals, "4k monitor", 300)
    assert [d.url for d in matched] == ["https://slickdeals.net/f/2"]
    assert [d.url for d in filter_deals(deals, "4k monitor", None)] == ["https://slickdeals.net/f/1", "https://slickdeals.net/f/2"]


def test_rss_with_entities_is_refused() -> None:
    bomb = b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><rss><channel><item><title>&lol;</title></item></channel></rss>'
    with pytest.raises(ValueError):
        parse_rss_deals(bomb)


def test_reddit_listing() -> None:
    payload = {
        "data": {
            "children": [
                {"data": {"title": "[Monitor] Dell S2722QC 27\" 4K USB-C - $229.99", "permalink": "/r/buildapcsales/comments/a/", "url": "https://www.dell.com/x", "created_utc": 1793000000}},
                {"data": {"title": "[Monitor] 4K pinned", "permalink": "/r/buildapcsales/comments/b/", "url": "https://x.example", "stickied": True}},
                {"data": {"title": "4K monitor discussion", "permalink": "/r/deals/comments/c/", "url": "https://www.reddit.com/r/deals/comments/c/", "is_self": True}},
                {"data": {"title": "nsfw", "permalink": "/r/deals/comments/d/", "over_18": True}},
            ]
        }
    }
    deals = parse_reddit_deals(payload)
    assert len(deals) == 2
    assert deals[0].url == "https://www.dell.com/x" and deals[0].discussion_url == "https://www.reddit.com/r/buildapcsales/comments/a/" and deals[0].price == 229.99
    assert deals[1].url == "https://www.reddit.com/r/deals/comments/c/"
    assert parse_reddit_deals({"nonsense": 1}) == [] and parse_reddit_deals(None) == []


def test_query_helpers() -> None:
    assert query_tokens("The 4K Monitor for gaming") == ["4k", "monitor", "gaming"]
    assert matches_query("Two 4K monitors on sale", ["4k", "monitor"])
    assert not matches_query("1440p monitor", ["4k", "monitor"])
    assert first_dollar_price("Was $1,099 now $899.5") == 1099.0
    assert "q=4k+monitor" in slickdeals_url("4k monitor") and "rss=1" in slickdeals_url("x")
    assert reddit_url("4k monitor").startswith("https://www.reddit.com/r/buildapcsales+deals/search.json?q=4k+monitor")


# ---------------------------------------------------------------------------
# Product lookups
# ---------------------------------------------------------------------------
UPC_PAYLOAD = {
    "code": "OK",
    "total": 1,
    "items": [
        {
            "ean": "0885909950805",
            "title": "Apple AirPods Pro",
            "brand": "Apple",
            "model": "MWP22AM/A",
            "images": ["http://insecure.example/a.jpg", "https://img.example/airpods.jpg"],
            "lowest_recorded_price": 159.99,
            "highest_recorded_price": 299,
            "offers": [
                {"merchant": "Best Buy", "domain": "bestbuy.com", "price": 199.99, "currency": "", "availability": "In Stock", "link": "https://www.upcitemdb.com/norob/alink/?id=1", "updated_t": 1792000000},
                {"merchant": "Best Buy", "price": 189.99, "link": "https://www.upcitemdb.com/norob/alink/?id=2"},
                {"merchant": "Walmart", "price": 0, "link": "https://www.upcitemdb.com/norob/alink/?id=3"},
                {"merchant": "Sketchy", "price": 10, "link": "http://10.0.0.1/"},
            ],
        }
    ],
}


def test_upcitemdb_parsing() -> None:
    product = parse_upcitemdb(UPC_PAYLOAD)
    assert product["title"] == "Apple AirPods Pro" and product["brand"] == "Apple" and product["model"] == "MWP22AM/A"
    assert product["image"] == "https://img.example/airpods.jpg"
    assert [(o["merchant"], o["price"]) for o in product["offers"]] == [("Best Buy", 189.99)]
    assert parse_upcitemdb({"code": "OK", "items": []}) is None and parse_upcitemdb({"code": "INVALID_UPC"}) is None


async def test_product_lookup_caches_and_reports_limits() -> None:
    calls: list[str] = []

    class Fetcher:
        status = None

        async def get_json(self, url, headers=None):
            calls.append(url)
            if self.status:
                raise FetchError("busy", status=self.status)
            return UPC_PAYLOAD

    fetcher = Fetcher()
    lookup = ProductLookup(fetcher)  # type: ignore[arg-type]
    first = await lookup.lookup("885909950805")
    again = await lookup.lookup("0885909950805")
    assert first == again and len(calls) == 1 and calls[0].endswith("upc=885909950805")
    assert await lookup.lookup("ABC123") is None
    fetcher.status = 429
    with pytest.raises(LookupLimit):
        await lookup.lookup("036000291452")
    fetcher.status = 400
    assert await lookup.lookup("036000291452") is None


def test_search_links() -> None:
    links = {link["name"]: link["url"] for link in search_links("885909950805")}
    assert links["Amazon"] == "https://www.amazon.com/s?k=885909950805"
    assert links["CamelCamelCamel"] == "https://camelcamelcamel.com/search?sq=885909950805"
    assert set(links) == {"Amazon", "Walmart", "Best Buy", "Target", "Google Shopping", "CamelCamelCamel"}
    assert search_links("  ") == []
    assert price_tracker.BROWSER_HEADERS["User-Agent"].startswith("Mozilla/5.0")
