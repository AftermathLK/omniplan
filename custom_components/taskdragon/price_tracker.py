"""Price extraction, deal feeds and product lookups for the wishlist & price tracker.

Everything that leaves Home Assistant goes through :class:`GuardedFetcher`:

* only ``http``/``https`` on the standard ports, never credentials in the URL;
* host names must resolve to public addresses. :class:`PublicResolver` drops
  loopback, private, link-local, CGNAT and other non-global answers inside the
  connection itself, so neither a typed URL, a redirect (every hop is checked
  again) nor a DNS answer that changes between checks can reach the LAN or Home
  Assistant;
* a 10 second timeout, at most five redirects, a size cap applied after
  decompression, no cookies kept, and one request at a time per host with a gap
  in between.

Pages are parsed with BeautifulSoup's pure-Python parser (scripts never run) in
the executor, through a four-stage cascade: schema.org JSON-LD, product meta
tags, built-in selectors for large retailers, then (or first, when set) a CSS
selector the user supplied for a niche shop.

Deal radar reads the public Slickdeals RSS search and Reddit's JSON search for
deal subreddits; product lookups by UPC use UPCitemdb's free trial endpoint.
Only the search words or the barcode digits are sent.
"""

from __future__ import annotations

import asyncio
import codecs
import ipaddress
import json
import logging
import re
import socket
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus, urlencode, urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from bs4 import BeautifulSoup

from .barcodes import BarcodeError, display_barcode, lookup_gtin, normalize_barcode
from .const import REDDIT_DEAL_SUBS, REDDIT_SEARCH_URL, SLICKDEALS_RSS_URL, UPCITEMDB_URL, VERSION

_LOGGER = logging.getLogger(__name__)

MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_FEED_BYTES = 1024 * 1024
MAX_REDIRECTS = 5
REQUEST_TIMEOUT_SECONDS = 10
HOST_GAP_SECONDS = 3.0
MAX_PRICE = 10_000_000
MAX_URL_LENGTH = 2048
MAX_SELECTOR_LENGTH = 300
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}
API_USER_AGENT = f"TaskDragon/{VERSION} (Home Assistant integration; https://github.com/AftermathLK/taskdragon)"
# Names that only ever mean "somewhere on this network".
PRIVATE_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".home.arpa", ".corp", ".intranet", ".localdomain")
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
MAX_TRACKED_HOSTS = 512


class UnsafeUrl(ValueError):
    """The URL may not be fetched (wrong scheme, port, or a non-public host)."""


class FetchError(Exception):
    """A page or feed could not be fetched."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def blocked(self) -> bool:
        """The site refused an automated request (or asked to slow down)."""
        return self.status in (401, 403, 429, 503)


class LookupLimit(FetchError):
    """The product database's free daily allowance is used up."""


# ---------------------------------------------------------------------------
# URL safety
# ---------------------------------------------------------------------------
# IPv6 prefixes that carry an IPv4 address in their low 32 bits and are translated to it
# by a NAT64 gateway (RFC 6052 well-known prefix and RFC 8215 local-use prefix).
NAT64_NETWORKS = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


def _public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Public unicast only; IPv6 forms that embed an IPv4 address are judged by that address."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
        elif ip.teredo is not None:
            return False
        elif any(ip in net for net in NAT64_NETWORKS):
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global and not ip.is_multicast


def _public_host(host: str) -> bool:
    try:
        return _public_ip(ipaddress.ip_address(host.split("%", 1)[0]))
    except ValueError:
        return False


def check_url(url: Any) -> str:
    """Return ``url`` (trimmed) when it is a public http(s) address; raise :class:`UnsafeUrl` otherwise."""
    text = str(url or "").strip()
    if not text:
        raise UnsafeUrl("Enter a web address")
    if len(text) > MAX_URL_LENGTH:
        raise UnsafeUrl("That web address is too long")
    if any(ord(c) < 33 for c in text):
        raise UnsafeUrl("Web addresses can't contain spaces or control characters")
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https"):
        raise UnsafeUrl("Only http:// and https:// addresses can be tracked")
    if parts.username is not None or parts.password is not None:
        raise UnsafeUrl("Web addresses with a user name or password can't be tracked")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeUrl("That web address has no host name")
    try:
        port = parts.port
    except ValueError as err:
        raise UnsafeUrl("That web address has an invalid port") from err
    if port not in (None, 80, 443):
        raise UnsafeUrl("Only the standard web ports (80 and 443) can be tracked")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if not _public_ip(ip):
            raise UnsafeUrl("Addresses on your own network can't be tracked")
        return text
    if host == "localhost" or "." not in host or host.endswith(PRIVATE_SUFFIXES):
        raise UnsafeUrl("Addresses on your own network can't be tracked")
    return text


def check_selector(selector: Any) -> str | None:
    """Validate a user's custom price selector (``css`` or ``css@attribute``); None when blank."""
    text = str(selector or "").strip()
    if not text:
        return None
    if len(text) > MAX_SELECTOR_LENGTH:
        raise ValueError("That selector is too long")
    css, attr = _split_selector(text)
    if attr is not None and not re.match(r"^[A-Za-z_:][-A-Za-z0-9_:.]{0,63}$", attr):
        raise ValueError("The attribute after @ must be a plain attribute name, like content or data-price")
    try:
        BeautifulSoup("<p></p>", "html.parser").select_one(css)
    except Exception as err:  # noqa: BLE001 - the selector engine raises its own SelectorSyntaxError and ValueErrors
        raise ValueError("That isn't a valid CSS selector") from err
    return text


def _split_selector(text: str) -> tuple[str, str | None]:
    """``div.price@data-value`` -> (``div.price``, ``data-value``); an @ inside [...] is part of the CSS."""
    depth = 0
    for index in range(len(text) - 1, -1, -1):
        char = text[index]
        if char == "]":
            depth += 1
        elif char == "[":
            depth -= 1
        elif char == "@" and depth == 0:
            return text[:index].strip(), text[index + 1 :].strip() or None
    return text, None


class PublicResolver(AbstractResolver):
    """DNS resolution that only ever answers with public addresses."""

    def __init__(self) -> None:
        self._inner = aiohttp.ThreadedResolver()

    async def resolve(self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET) -> list[Any]:
        results = await self._inner.resolve(host, port, family)
        public = [result for result in results if _public_host(str(result["host"]))]
        if not public:
            raise OSError(f"{host} does not resolve to a public address")
        return public

    async def close(self) -> None:
        await self._inner.close()


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
@dataclass
class Page:
    url: str
    status: int
    body: bytes
    charset: str | None
    content_type: str
    truncated: bool = False


class GuardedFetcher:
    """The only way the tracker talks to the internet (see the module docstring)."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], aiohttp.ClientSession] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
        host_gap: float = HOST_GAP_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._session: aiohttp.ClientSession | None = None
        self._monotonic = monotonic
        self._sleep = sleep
        self._host_gap = host_gap
        self._host_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._host_last: OrderedDict[str, float] = OrderedDict()

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            if self._session_factory is not None:
                self._session = self._session_factory()
            else:
                connector = aiohttp.TCPConnector(resolver=PublicResolver(), limit=4, limit_per_host=1, ttl_dns_cache=300)
                self._session = aiohttp.ClientSession(
                    connector=connector,
                    cookie_jar=aiohttp.DummyCookieJar(),
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
                    trust_env=False,
                )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _host_lock(self, host: str) -> asyncio.Lock:
        """The per-host lock; the least recently used idle hosts are forgotten past MAX_TRACKED_HOSTS."""
        lock = self._host_locks.get(host)
        if lock is None:
            lock = self._host_locks[host] = asyncio.Lock()
        self._host_locks.move_to_end(host)
        if len(self._host_locks) > MAX_TRACKED_HOSTS:
            for name in [n for n, held in self._host_locks.items() if not held.locked()][: len(self._host_locks) - MAX_TRACKED_HOSTS]:
                self._host_locks.pop(name, None)
                self._host_last.pop(name, None)
        return lock

    async def _wait_turn(self, host: str) -> None:
        last = self._host_last.get(host)
        if last is not None:
            wait = self._host_gap - (self._monotonic() - last)
            if wait > 0:
                await self._sleep(wait)

    async def get(self, url: str, *, headers: Mapping[str, str], max_bytes: int, truncate: bool = False) -> Page:
        """GET ``url``, following up to five redirects (each re-checked); raises :class:`FetchError`."""
        try:
            current = check_url(url)
        except UnsafeUrl as err:
            raise FetchError(str(err)) from err
        session = self._get_session()
        for _hop in range(MAX_REDIRECTS + 1):
            host = (urlsplit(current).hostname or "").lower()
            lock = self._host_lock(host)
            async with lock:
                await self._wait_turn(host)
                try:
                    async with session.get(current, headers=dict(headers), allow_redirects=False) as response:
                        self._host_last[host] = self._monotonic()
                        if response.status in REDIRECT_STATUSES:
                            location = response.headers.get("Location")
                            if not location:
                                raise FetchError(f"{host} sent a redirect without a destination", status=response.status)
                            try:
                                current = check_url(urljoin(current, location))
                            except UnsafeUrl as err:
                                raise FetchError(f"{host} redirected somewhere that can't be tracked: {err}") from err
                            continue
                        if response.status != 200:
                            raise FetchError(f"{host} answered {response.status}", status=response.status)
                        body, truncated = await _read_limited(response, max_bytes, truncate)
                        return Page(str(response.url), response.status, body, response.charset, response.content_type or "", truncated)
                except FetchError:
                    raise
                except asyncio.TimeoutError as err:
                    self._host_last[host] = self._monotonic()
                    raise FetchError(f"{host} took too long to answer") from err
                except (aiohttp.ClientError, OSError) as err:
                    self._host_last[host] = self._monotonic()
                    raise FetchError(f"{host} could not be reached") from err
        raise FetchError("The page redirected too many times")

    async def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
        page = await self.get(url, headers={"User-Agent": API_USER_AGENT, "Accept": "application/json", **(headers or {})}, max_bytes=MAX_FEED_BYTES)
        try:
            return json.loads(page.body.decode(_codec(page.charset), errors="replace"))
        except ValueError as err:
            raise FetchError("The answer was not JSON") from err


def _codec(charset: str | None) -> str:
    """The response's charset when Python knows it, UTF-8 otherwise."""
    try:
        return codecs.lookup(charset or "utf-8").name
    except LookupError:
        return "utf-8"


async def _read_limited(response: aiohttp.ClientResponse, max_bytes: int, truncate: bool) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(65536):
        total += len(chunk)
        if total > max_bytes:
            if not truncate:
                raise FetchError("The page is larger than allowed")
            chunks.append(chunk[: max_bytes - (total - len(chunk))])
            return b"".join(chunks), True
        chunks.append(chunk)
    return b"".join(chunks), False


# ---------------------------------------------------------------------------
# Prices and availability
# ---------------------------------------------------------------------------
_NUMBER_RE = re.compile(r"\d[\d.,  ' ]*")
_CURRENCY_SIGNS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR", "₩": "KRW"}
_IN_STOCK = {"instock", "in stock", "limitedavailability", "onlineonly", "instoreonly", "preorder", "presale", "backorder", "available", "in_stock", "true"}
_OUT_OF_STOCK = {"outofstock", "out of stock", "soldout", "sold out", "discontinued", "out_of_stock", "unavailable", "currently unavailable", "oos", "false"}


def parse_price(value: Any) -> float | None:
    """A positive price from ``19.99``, ``"$1,299.99"``, ``"1.299,99 €"`` or ``"USD 249"``; None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return round(number, 2) if 0 < number < MAX_PRICE else None
    text = str(value or "").strip()[:200]
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    raw = re.sub(r"[  ' ]", "", match.group(0)).rstrip(".,")
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        head, _, tail = raw.rpartition(",")
        raw = head.replace(",", "") + "." + tail if raw.count(",") == 1 and len(tail) == 2 else raw.replace(",", "")
    elif raw.count(".") > 1:
        head, _, tail = raw.rpartition(".")
        raw = raw.replace(".", "") if len(tail) == 3 else head.replace(".", "") + "." + tail
    try:
        number = float(raw)
    except ValueError:
        return None
    return round(number, 2) if 0 < number < MAX_PRICE else None


def currency_from_text(text: Any) -> str | None:
    value = str(text or "")
    for sign, code in _CURRENCY_SIGNS.items():
        if sign in value:
            return code
    match = re.search(r"\b([A-Z]{3})\b", value)
    return match.group(1) if match else None


def normalize_availability(value: Any) -> bool | None:
    """True (can be bought now), False (sold out) or None (unknown) from schema.org URLs or shop wording."""
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return None
    tail = text.rstrip("/").rsplit("/", 1)[-1]
    if tail in _IN_STOCK or text in _IN_STOCK:
        return True
    if tail in _OUT_OF_STOCK or text in _OUT_OF_STOCK:
        return False
    if any(word in text for word in ("out of stock", "sold out", "unavailable", "no longer available", "discontinued")):
        return False
    if "in stock" in text or "add to cart" in text or "ships" in text:
        return True
    return None


@dataclass
class Extraction:
    price: float | None = None
    currency: str | None = None
    in_stock: bool | None = None
    method: str | None = None
    title: str | None = None
    image: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _types(node: Mapping[str, Any]) -> set[str]:
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return {str(v).rsplit("/", 1)[-1] for v in values if isinstance(v, str)}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _walk(node: Any, depth: int = 0, budget: list[int] | None = None) -> Iterator[Mapping[str, Any]]:
    """Every JSON object inside ``node`` (depth- and size-bounded)."""
    budget = budget if budget is not None else [5000]
    if depth > 14 or budget[0] <= 0:
        return
    if isinstance(node, list):
        for child in node:
            yield from _walk(child, depth + 1, budget)
    elif isinstance(node, dict):
        budget[0] -= 1
        yield node
        for child in node.values():
            if isinstance(child, (list, dict)):
                yield from _walk(child, depth + 1, budget)


def _image_url(value: Any, base: str) -> str | None:
    for candidate in _as_list(value):
        if isinstance(candidate, Mapping):
            candidate = candidate.get("url") or candidate.get("contentUrl")
        if isinstance(candidate, str) and candidate.strip():
            absolute = urljoin(base, candidate.strip())
            if absolute.lower().startswith("https://"):
                return absolute[:MAX_URL_LENGTH]
    return None


def _offer_quotes(offers: Any) -> list[tuple[float, str | None, bool | None]]:
    quotes: list[tuple[float, str | None, bool | None]] = []
    for offer in _as_list(offers)[:200]:
        if not isinstance(offer, Mapping):
            continue
        kinds = _types(offer)
        price = parse_price(offer.get("lowPrice")) if "AggregateOffer" in kinds else None
        if price is None:
            price = parse_price(offer.get("price"))
        currency = offer.get("priceCurrency")
        if price is None:
            for spec in _as_list(offer.get("priceSpecification")):
                if isinstance(spec, Mapping) and parse_price(spec.get("price")) is not None:
                    price = parse_price(spec.get("price"))
                    currency = currency or spec.get("priceCurrency")
                    break
        availability = normalize_availability(offer.get("availability"))
        if price is not None:
            quotes.append((price, str(currency)[:8] if currency else None, availability))
        if "AggregateOffer" in kinds and offer.get("offers"):
            quotes.extend(_offer_quotes(offer.get("offers")))
    return quotes


def _best_quote(quotes: list[tuple[float, str | None, bool | None]]) -> tuple[float, str | None, bool | None] | None:
    if not quotes:
        return None
    buyable = [q for q in quotes if q[2] is not False]
    pool = buyable or quotes
    return min(pool, key=lambda q: q[0])


def _jsonld_documents(soup: BeautifulSoup) -> Iterator[Any]:
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        text = (tag.string or tag.get_text() or "").strip()
        if not text or len(text) > 2_000_000:
            continue
        text = re.sub(r"^\s*(<!--|//\s*<!\[CDATA\[)|(-->|//\s*\]\]>)\s*$", "", text).strip()
        try:
            yield json.loads(text)
        except ValueError:
            try:
                yield json.loads(re.sub(r",\s*([}\]])", r"\1", text))
            except ValueError:
                continue


def _from_jsonld(soup: BeautifulSoup, base: str, out: Extraction) -> bool:
    products: list[Mapping[str, Any]] = []
    loose_offers: list[Mapping[str, Any]] = []
    for document in _jsonld_documents(soup):
        for node in _walk(document):
            kinds = _types(node)
            if kinds & {"Product", "ProductGroup", "IndividualProduct", "ProductModel", "Vehicle"}:
                products.append(node)
            elif kinds & {"Offer", "AggregateOffer"}:
                loose_offers.append(node)
    for product in products:
        out.title = out.title or _clean_text(product.get("name"))
        out.image = out.image or _image_url(product.get("image"), base)
        quotes = _offer_quotes(product.get("offers"))
        if not quotes and "ProductGroup" in _types(product):
            for variant in _as_list(product.get("hasVariant"))[:100]:
                if isinstance(variant, Mapping):
                    quotes.extend(_offer_quotes(variant.get("offers")))
        best = _best_quote(quotes)
        if best is not None:
            out.price, out.currency, out.in_stock = best
            out.method = "jsonld"
            return True
    best = _best_quote(_offer_quotes(loose_offers))
    if best is not None:
        out.price, out.currency, out.in_stock = best
        out.method = "jsonld"
        return True
    return False


def _meta(soup: BeautifulSoup, *pairs: tuple[str, str]) -> str | None:
    for attr, name in pairs:
        tag = soup.find("meta", attrs={attr: name})
        if tag is not None and tag.get("content"):
            return str(tag.get("content")).strip()
    return None


def _from_meta(soup: BeautifulSoup, out: Extraction) -> bool:
    price = parse_price(_meta(soup, ("property", "product:price:amount"), ("property", "og:price:amount"), ("name", "product:price:amount")))
    if price is None:
        tag = soup.select_one('[itemprop="price"]')
        if tag is not None:
            price = parse_price(tag.get("content") or tag.get_text(" ", strip=True))
    if price is None:
        label = (_meta(soup, ("name", "twitter:label1")) or "").lower()
        if "price" in label:
            price = parse_price(_meta(soup, ("name", "twitter:data1")))
    if price is None:
        return False
    out.price = price
    currency = _meta(soup, ("property", "product:price:currency"), ("property", "og:price:currency"))
    if not currency:
        tag = soup.select_one('[itemprop="priceCurrency"]')
        currency = (tag.get("content") or tag.get_text(strip=True)) if tag is not None else None
    out.currency = str(currency)[:8] if currency else out.currency
    availability = _meta(soup, ("property", "product:availability"), ("property", "og:availability"))
    if availability is None:
        tag = soup.select_one('[itemprop="availability"]')
        availability = (tag.get("href") or tag.get("content") or tag.get_text(strip=True)) if tag is not None else None
    out.in_stock = normalize_availability(availability)
    out.method = "meta"
    return True


@dataclass(frozen=True)
class RetailerRule:
    """Built-in selectors for a large retailer (tried in order)."""

    name: str
    domains: tuple[str, ...]
    price: tuple[str, ...] = ()
    availability: tuple[str, ...] = ()
    split_cents: bool = False  # the price is rendered as "$" "249" "97" in separate elements


RETAILERS: tuple[RetailerRule, ...] = (
    RetailerRule(
        "Amazon",
        ("amazon.com", "amazon.ca", "amazon.co.uk", "amazon.de", "amazon.fr", "amazon.it", "amazon.es", "amazon.com.au", "amazon.co.jp", "amazon.com.mx", "amazon.in"),
        price=(
            "#corePrice_feature_div .a-price .a-offscreen",
            "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
            "#apex_desktop .a-price .a-offscreen",
            "#priceblock_dealprice",
            "#priceblock_ourprice",
            ".a-price .a-offscreen",
        ),
        availability=("#availability",),
    ),
    RetailerRule(
        "Best Buy",
        ("bestbuy.com", "bestbuy.ca"),
        price=('[data-testid="customer-price"] span', ".priceView-customer-price span", ".priceView-hero-price span"),
        availability=(".fulfillment-add-to-cart-button button", "button.add-to-cart-button"),
    ),
    RetailerRule(
        "Walmart",
        ("walmart.com", "walmart.ca"),
        price=('[itemprop="price"]', 'span[data-automation-id="product-price"] span', '[data-testid="price-wrap"] span'),
        availability=('[data-automation-id="atc"]', '[data-testid="add-to-cart-section"]'),
    ),
    RetailerRule("Target", ("target.com",), price=('[data-test="product-price"]', '[data-test="product-price-sale"]'), availability=('[data-test="fulfillment-cell-shipping"]',)),
    RetailerRule("Home Depot", ("homedepot.com", "homedepot.ca"), price=(".price-format__main-price", '[data-testid="price-format"]'), split_cents=True),
    RetailerRule("Lowe's", ("lowes.com", "lowes.ca"), price=('[data-testid="product-price"]', ".main-price", ".final-price")),
    RetailerRule("Menards", ("menards.com",), price=("#itemFinalPrice@data-final-price", "#itemFinalPrice", ".itemFinalPrice")),
    RetailerRule("Newegg", ("newegg.com", "newegg.ca"), price=(".product-buy-box .price-current", ".price-current")),
    RetailerRule("B&H", ("bhphotovideo.com",), price=('[data-selenium="pricingPrice"]',), availability=('[data-selenium="stockStatus"]',)),
    RetailerRule("Costco", ("costco.com", "costco.ca"), price=('[automation-id="productPriceOutput"]', ".your-price .value")),
    RetailerRule("eBay", ("ebay.com", "ebay.ca", "ebay.co.uk"), price=(".x-price-primary span.ux-textspans", "#prcIsum")),
    RetailerRule("Micro Center", ("microcenter.com",), price=("#pricing@content", "#pricing")),
    RetailerRule("Kohl's", ("kohls.com",), price=(".pdpprice-row2-main-text",)),
    RetailerRule("Staples", ("staples.com",), price=(".price-info__final_price_sku",)),
)
_RETAILER_NAMES = {
    "adorama.com": "Adorama", "apple.com": "Apple", "dell.com": "Dell", "lenovo.com": "Lenovo", "etsy.com": "Etsy",
    "rei.com": "REI", "samsclub.com": "Sam's Club", "officedepot.com": "Office Depot", "gamestop.com": "GameStop",
    "macys.com": "Macy's", "wayfair.com": "Wayfair", "ikea.com": "IKEA", "harborfreight.com": "Harbor Freight",
    "acehardware.com": "Ace Hardware", "tractorsupply.com": "Tractor Supply", "cabelas.com": "Cabela's",
    "basspro.com": "Bass Pro Shops", "dickssportinggoods.com": "Dick's", "chewy.com": "Chewy", "zappos.com": "Zappos",
    "nordstrom.com": "Nordstrom", "lego.com": "LEGO", "nintendo.com": "Nintendo", "playstation.com": "PlayStation",
    "aliexpress.com": "AliExpress", "temu.com": "Temu", "bestbuy.ca": "Best Buy", "canadiantire.ca": "Canadian Tire",
}


def _registrable(host: str) -> str:
    parts = host.lower().rstrip(".").split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "net", "org"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def retailer_rule(url: str) -> RetailerRule | None:
    host = (urlsplit(url).hostname or "").lower()
    for rule in RETAILERS:
        if any(host == domain or host.endswith("." + domain) for domain in rule.domains):
            return rule
    return None


def retailer_name(url: str) -> str:
    """A friendly store name for a product URL ("Best Buy", "Home Depot", or the site's own name)."""
    rule = retailer_rule(url)
    if rule is not None:
        return rule.name
    host = (urlsplit(url).hostname or "").lower()
    base = _registrable(host)
    if base in _RETAILER_NAMES:
        return _RETAILER_NAMES[base]
    label = base.split(".", 1)[0]
    return label[:1].upper() + label[1:] if label else "Store"


def _select_value(soup: BeautifulSoup, spec: str, split_cents: bool = False) -> str | None:
    css, attr = _split_selector(spec)
    try:
        tag = soup.select_one(css)
    except Exception:  # noqa: BLE001 - a stored selector that no longer parses just finds nothing
        return None
    if tag is None:
        return None
    if attr:
        value = tag.get(attr)
        return " ".join(value) if isinstance(value, list) else (str(value) if value is not None else None)
    if split_cents:
        numbers = [part for part in (s.strip() for s in tag.stripped_strings) if re.fullmatch(r"\d+", part)]
        if len(numbers) >= 2 and len(numbers[-1]) == 2:
            return "".join(numbers[:-1]) + "." + numbers[-1]
    if tag.name == "meta":
        return str(tag.get("content") or "")
    return tag.get("content") or tag.get_text(" ", strip=True)


def _from_selectors(soup: BeautifulSoup, specs: Iterable[str], out: Extraction, method: str, split_cents: bool = False) -> bool:
    for spec in specs:
        text = _select_value(soup, spec, split_cents)
        price = parse_price(text)
        if price is not None:
            out.price = price
            out.currency = out.currency or currency_from_text(text)
            out.method = method
            return True
    return False


def _clean_text(value: Any, limit: int = 300) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip()
    return text[:limit] or None


def extract_offer(body: bytes, url: str, charset: str | None = None, custom_selector: str | None = None) -> Extraction:
    """Price, currency, availability, title and image from a product page (blocking; run in the executor)."""
    soup = BeautifulSoup(body, "html.parser", from_encoding=charset)
    for tag in soup(["style", "svg", "noscript", "iframe"]):
        tag.decompose()
    out = Extraction()
    found = False
    if custom_selector:
        found = _from_selectors(soup, (custom_selector,), out, "custom")
    if not found:
        found = _from_jsonld(soup, url, out)
    if not found:
        found = _from_meta(soup, out)
    rule = retailer_rule(url)
    if not found and rule is not None:
        found = _from_selectors(soup, rule.price, out, "retailer", rule.split_cents)
    if out.in_stock is None and rule is not None:
        for spec in rule.availability:
            text = _select_value(soup, spec)
            if text:
                out.in_stock = normalize_availability(text)
                if out.in_stock is not None:
                    break
    out.title = out.title or _clean_text(_meta(soup, ("property", "og:title"), ("name", "twitter:title"))) or _clean_text(soup.title.string if soup.title else None)
    out.image = out.image or _image_url(_meta(soup, ("property", "og:image"), ("property", "og:image:secure_url"), ("name", "twitter:image")), url)
    if out.currency:
        out.currency = str(out.currency).strip().upper()[:8] or None
    return out


# ---------------------------------------------------------------------------
# Deal radar
# ---------------------------------------------------------------------------
_STOP_WORDS = frozenset({"the", "a", "an", "and", "or", "for", "with", "of", "to", "in", "on", "new"})
_DEAL_PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?")


@dataclass
class Deal:
    source: str
    title: str
    url: str
    discussion_url: str | None = None
    price: float | None = None
    posted_at: str | None = None

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


def query_tokens(query: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", str(query or "").lower())
    return [t for t in dict.fromkeys(tokens) if t not in _STOP_WORDS and (len(t) > 1 or t.isdigit())][:8]


def matches_query(title: str, tokens: list[str]) -> bool:
    """Every search word appears in the title (a word may be the start of a longer one: monitor ~ monitors)."""
    if not tokens:
        return False
    words = re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", title.lower())
    return all(any(word == token or word.startswith(token) for word in words) for token in tokens)


def first_dollar_price(text: str) -> float | None:
    match = _DEAL_PRICE_RE.search(text or "")
    if not match:
        return None
    whole = match.group(1).replace(",", "")
    cents = (match.group(2) or "").ljust(2, "0") if match.group(2) else "00"
    return parse_price(f"{whole}.{cents}")


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def slickdeals_url(query: str) -> str:
    params = {"mode": "frontpage", "searcharea": "deals", "searchin": "first", "rss": "1", "q": query}
    return SLICKDEALS_RSS_URL + "?" + urlencode(params)


def reddit_url(query: str, subs: Iterable[str] = REDDIT_DEAL_SUBS) -> str:
    params = {"q": query, "restrict_sr": "1", "sort": "new", "t": "month", "limit": "25"}
    return REDDIT_SEARCH_URL.format(subs="+".join(subs)) + "?" + urlencode(params)


def parse_rss_deals(body: bytes, source: str = "slickdeals") -> list[Deal]:
    """Items of an RSS 2.0 feed; documents with a DTD or entities are refused outright."""
    head = body[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in body.upper():
        raise ValueError("Feeds with a document type declaration are not read")
    root = ET.fromstring(body)
    deals: list[Deal] = []
    for item in root.iter("item"):
        title = _clean_text(item.findtext("title"), 300)
        link = (item.findtext("link") or "").strip()
        if not title or not link.lower().startswith(("https://", "http://")):
            continue
        posted = None
        raw_date = item.findtext("pubDate")
        if raw_date:
            try:
                posted = _iso(parsedate_to_datetime(raw_date))
            except (TypeError, ValueError):
                posted = None
        description = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
        price = first_dollar_price(title) or first_dollar_price(description)
        deals.append(Deal(source=source, title=title, url=link[:MAX_URL_LENGTH], discussion_url=link[:MAX_URL_LENGTH], price=price, posted_at=posted))
        if len(deals) >= 50:
            break
    return deals


def parse_reddit_deals(payload: Any) -> list[Deal]:
    """Posts of a Reddit listing; NSFW, pinned and removed posts are skipped."""
    deals: list[Deal] = []
    children = (((payload or {}).get("data") or {}).get("children") or []) if isinstance(payload, Mapping) else []
    for child in children[:100]:
        data = child.get("data") if isinstance(child, Mapping) else None
        if not isinstance(data, Mapping) or data.get("over_18") or data.get("stickied") or data.get("removed_by_category"):
            continue
        title = _clean_text(data.get("title"), 300)
        permalink = str(data.get("permalink") or "")
        if not title or not permalink.startswith("/r/"):
            continue
        discussion = "https://www.reddit.com" + permalink[:500]
        link = str(data.get("url_overridden_by_dest") or data.get("url") or "")
        host = (urlsplit(link).hostname or "").lower()
        if not link.lower().startswith(("https://", "http://")) or host.endswith(("reddit.com", "redd.it")):
            link = discussion
        created = data.get("created_utc")
        posted = _iso(datetime.fromtimestamp(float(created), UTC)) if isinstance(created, (int, float)) else None
        deals.append(Deal(source="reddit", title=title, url=link[:MAX_URL_LENGTH], discussion_url=discussion, price=first_dollar_price(title), posted_at=posted))
    return deals


def filter_deals(deals: Iterable[Deal], query: str, max_price: float | None) -> list[Deal]:
    tokens = query_tokens(query)
    out = []
    for deal in deals:
        if not matches_query(deal.title, tokens):
            continue
        if max_price is not None and deal.price is not None and deal.price > max_price:
            continue
        out.append(deal)
    return out


# ---------------------------------------------------------------------------
# Product lookup by barcode
# ---------------------------------------------------------------------------
def search_links(query: str) -> list[dict[str, str]]:
    """One-tap searches for a UPC or model number at big online stores."""
    q = quote_plus(str(query or "").strip()[:120])
    if not q:
        return []
    return [
        {"name": "Amazon", "url": f"https://www.amazon.com/s?k={q}"},
        {"name": "Walmart", "url": f"https://www.walmart.com/search?q={q}"},
        {"name": "Best Buy", "url": f"https://www.bestbuy.com/site/searchpage.jsp?st={q}"},
        {"name": "Target", "url": f"https://www.target.com/s?searchTerm={q}"},
        {"name": "Google Shopping", "url": f"https://www.google.com/search?tbm=shop&q={q}"},
        {"name": "CamelCamelCamel", "url": f"https://camelcamelcamel.com/search?sq={q}"},
    ]


def parse_upcitemdb(payload: Any) -> dict[str, Any] | None:
    """Title, brand, model, image and recorded offers from a UPCitemdb lookup (None when unknown)."""
    if not isinstance(payload, Mapping) or payload.get("code") != "OK":
        return None
    items = payload.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], Mapping):
        return None
    item = items[0]
    title = _clean_text(item.get("title"), 200)
    if not title:
        return None
    offers: dict[str, dict[str, Any]] = {}
    for offer in item.get("offers") or []:
        if not isinstance(offer, Mapping):
            continue
        price = parse_price(offer.get("price"))
        link = str(offer.get("link") or "")
        merchant = _clean_text(offer.get("merchant"), 80)
        if price is None or not merchant or not link.lower().startswith("https://"):
            continue
        try:
            check_url(link)
        except UnsafeUrl:
            continue
        updated = offer.get("updated_t")
        entry = {
            "merchant": merchant,
            "domain": _clean_text(offer.get("domain"), 120),
            "price": price,
            "list_price": parse_price(offer.get("list_price")),
            "currency": _clean_text(offer.get("currency"), 8),
            "availability": _clean_text(offer.get("availability"), 40),
            "in_stock": normalize_availability(offer.get("availability")),
            "condition": _clean_text(offer.get("condition"), 40),
            "link": link[:MAX_URL_LENGTH],
            "updated_at": _iso(datetime.fromtimestamp(float(updated), UTC)) if isinstance(updated, (int, float)) and updated > 0 else None,
        }
        key = merchant.lower()
        if key not in offers or price < offers[key]["price"]:
            offers[key] = entry
    images = [str(u) for u in item.get("images") or [] if isinstance(u, str) and u.lower().startswith("https://")]
    return {
        "title": title,
        "brand": _clean_text(item.get("brand"), 120),
        "model": _clean_text(item.get("model"), 120),
        "description": _clean_text(item.get("description"), 600),
        "category": _clean_text(item.get("category"), 200),
        "image": images[0][:MAX_URL_LENGTH] if images else None,
        "lowest_recorded_price": parse_price(item.get("lowest_recorded_price")),
        "highest_recorded_price": parse_price(item.get("highest_recorded_price")),
        "offers": sorted(offers.values(), key=lambda o: o["price"])[:20],
        "source": "UPCitemdb",
    }


class ProductLookup:
    """UPCitemdb lookups with a cache (the free tier allows about 100 a day)."""

    FOUND_SECONDS = 24 * 3600
    MISSING_SECONDS = 6 * 3600
    CACHE_ENTRIES = 300

    def __init__(self, fetcher: GuardedFetcher, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._fetcher = fetcher
        self._monotonic = monotonic
        self._cache: OrderedDict[str, tuple[dict[str, Any] | None, float]] = OrderedDict()

    async def lookup(self, code: str) -> dict[str, Any] | None:
        try:
            gtin = lookup_gtin(normalize_barcode(code)[0])
        except BarcodeError:
            return None
        if gtin is None:
            return None
        cached = self._cache.get(gtin)
        if cached is not None and self._monotonic() - cached[1] < (self.FOUND_SECONDS if cached[0] else self.MISSING_SECONDS):
            self._cache.move_to_end(gtin)
            return cached[0]
        query = display_barcode(gtin)
        try:
            payload = await self._fetcher.get_json(UPCITEMDB_URL + "?" + urlencode({"upc": query}))
        except FetchError as err:
            if err.status == 429:
                raise LookupLimit("The free UPC lookup allowance is used up for today", status=429) from err
            if err.status in (400, 404):
                payload = None
            else:
                raise
        product = parse_upcitemdb(payload)
        self._cache[gtin] = (product, self._monotonic())
        while len(self._cache) > self.CACHE_ENTRIES:
            self._cache.popitem(last=False)
        return product
