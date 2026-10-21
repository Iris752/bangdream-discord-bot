"""Multi-source BanG Dream official news watcher. No generative AI or paid APIs."""
from __future__ import annotations
import hashlib
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag, parse_qsl, urlencode, urlunparse, quote

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'state.json'
SOURCES = ROOT / 'sources.json'
LOG = logging.getLogger('bangdream')
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; BangDreamPersonalNewsWatcher/2.0)'})
TIMEOUT = 8
RSS_TIMEOUT = 5
BACKFILL_MAX_PER_RUN = 5
BACKFILL_SCAN_PER_SOURCE = 3
MAX_PER_SOURCE = 25
MAX_POSTS_PER_RUN = 5
MAX_POSTS_PER_DAY = 24
MAX_SEEN = 12000
MAX_AGE_DAYS = 7
UNKNOWN_DATE_TOP_N = 3  # Unknown dates: only brand-new URLs near the top of a listing
JST = ZoneInfo("Asia/Tokyo")
# Include project-wide stories even if no band is named. Apply filtering only to general corporate feeds.
RELATED = ('bang dream', 'bangdream', 'バンドリ', 'ガルパ', 'mygo', 'ave mujica', 'roselia',
           'poppin', 'afterglow', 'pastel', 'morfonica', 'raise a suilen', 'ハロー、ハッピー',
           '夢限大みゅーたいぷ', 'millsage', '一家dumb rock', 'our notes', 'アワーノーツ', '夢ノ結唱', 'poppin’party', 'パスパレ', 'ポピパ', 'ラス', 'ハロハピ', 'ミルサージュ')
# Exclude personal activities only on broad sources; do not discard official band announcements
# simply because they mention a cast member.
PERSONAL = ('個人イベント', 'ソロイベント', '個人名義', 'ソロ活動', '声優個人', '個人出演')
DETAIL = re.compile(r'(開催|日時|日程|会場|場所|期間|発売|販売|価格|料金|チケット|受付|応募|申込|締切|特典|注意|配信|対象|出演|詳細|メンテナンス|アップデート|\d{1,2}月\d{1,2}日|20\d{2}年)', re.I)


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(url):
    url, _ = urldefrag(url)
    p = urlparse(url)
    qs = [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in ('utm_source', 'utm_medium', 'utm_campaign', 'fbclid', 'gclid')]
    return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/') + ('' if re.search(r'\.[a-z0-9]{2,5}$', p.path, re.I) else '/'), '', urlencode(qs), ''))


def fetch(url):
    for i in range(2):
        try:
            r = SESSION.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            if len(r.content) > 5_000_000:
                raise ValueError('Page too large')
            return BeautifulSoup(r.content, 'lxml')
        except (requests.RequestException, ValueError):
            if i == 1:
                raise
            time.sleep(i + 1)


def article_link(url, source):
    p, base = urlparse(url), urlparse(source['url'])
    if p.scheme not in ('http', 'https') or p.netloc != base.netloc:
        return False
    path = p.path.rstrip('/').lower()
    if path == base.path.rstrip('/').lower():
        return False
    if re.search(r'/(page|category|tag|author|search|privacy|contact)(/|$)', path):
        return False
    if re.search(r'\.(png|jpg|webp|gif|pdf|zip|svg)$', path):
        return False
    return bool(re.search(source['article_pattern'], path, re.I))


def rss_items(endpoint):
    """Read RSS/Atom links and original timestamps where available."""
    response = SESSION.get(endpoint, timeout=RSS_TIMEOUT)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    items = []
    for node in root.findall('.//item'):
        link = node.findtext('link', '').strip()
        date = node.findtext('pubDate', '').strip()
        if link:
            items.append((link, date))
    ns = {'a': 'http://www.w3.org/2005/Atom'}
    for node in root.findall('.//a:entry', ns):
        linknode = node.find('a:link', ns)
        link = linknode.get('href', '') if linknode is not None else ''
        date = node.findtext('a:published', '', ns)
        if link:
            items.append((link, date))
    return items


def discover(source):
    """HTML, RSS, search fallback and Shopify store products (no paid API)."""
    found, visited, failures = [], set(), []
    mode = source.get('discovery', 'html')
    name = source['name']
    def add(u):
        u = canonical(u)
        if u not in visited and article_link(u, source):
            visited.add(u)
            found.append(u)
    if mode in ('html', 'hybrid', 'search_rss', 'shopify'):
        listings = [source['url']] + source.get('additional_list_pages', [])
        for listing in listings:
            try:
                soup = fetch(listing)
                for a in soup.select('a[href]'):
                    add(urljoin(listing, a['href']))
            except Exception as exc:
                failures.append(f'HTML {listing}: {exc}')
            time.sleep(0.15)
    for listing in source.get('search_pages', []):
        try:
            soup = fetch(listing)
            for a in soup.select('a[href]'):
                add(urljoin(listing, a['href']))
        except Exception as exc:
            failures.append(f'Search page {listing}: {exc}')
    # Shopify product catalog: use the public JSON endpoint, if enabled by store.
    if source.get('discovery') == 'shopify' and source.get('shopify_api', False):
        endpoint = source['url'].rstrip('/') + '/products.json?limit=100'
        try:
            response = SESSION.get(endpoint, timeout=TIMEOUT)
            response.raise_for_status()
            products = response.json().get('products', [])
            for product in products:
                handle = product.get('handle')
                if handle:
                    add(urljoin(source['url'], '/products/' + handle))
            LOG.info('%s: Shopify API returned %d products', name, len(products))
        except Exception as exc:
            failures.append(f'Shopify catalog: {exc}')
        # Product pages sometimes are linked directly from home/collections.
        try:
            soup = fetch(source['url'])
            for a in soup.select('a[href*="/products/"]'):
                add(urljoin(source['url'], a['href']))
        except Exception as exc:
            failures.append(f'Shopify HTML: {exc}')
    for feed in source.get('rss_urls', []):
        try:
            for link, _ in rss_items(feed):
                add(link)
        except Exception as exc:
            failures.append(f'Official RSS {feed}: {exc}')
    if mode in ('search_rss', 'hybrid', 'shopify'):
        domain = urlparse(source['url']).netloc
        terms = source.get('search_terms', ['バンドリ', 'BanG Dream'])[:1]
        for term in terms:
            query = f'site:{domain} "{term}"'
            endpoints = [
                'https://www.bing.com/search?format=rss&q=' + quote(query),
            ]
            for endpoint in endpoints:
                try:
                    for link, date in rss_items(endpoint):
                        # Google News often returns redirect URLs; resolve only if needed.
                        if urlparse(link).netloc.endswith('news.google.com'):
                            try:
                                redirect = SESSION.get(link, timeout=TIMEOUT)
                                link = redirect.url
                            except requests.RequestException:
                                continue
                        add(link)
                except Exception as exc:
                    failures.append(f'RSS {domain}: {type(exc).__name__}: {exc}')
                time.sleep(0.1)
    if not found:
        raise RuntimeError('No article URLs discovered; ' + '; '.join(failures[:2]))
    if failures:
        LOG.warning('%s: partial discovery issue: %s', name, '; '.join(failures[:2]))
    return found


def clean(text):
    return re.sub(r'\s+', ' ', text).strip()


def extract(url):
    soup = fetch(url)
    title_tag = soup.select_one('meta[property="og:title"]')
    title = clean(title_tag.get('content', '')) if title_tag else ''
    if not title:
        node = soup.select_one('h1') or soup.select_one('title')
        title = clean(node.get_text(' ', strip=True)) if node else url
    for node in soup.select('script,style,nav,footer,header,aside,form,.breadcrumb,.share,.sns,.related,.sidebar'):
        node.decompose()
    candidates = [soup.select_one(x) for x in ('article', '.news-detail', '.news_detail', '.entry-content', '.post-content', 'main')]
    area = next((n for n in candidates if n and len(n.get_text(' ', strip=True)) >= 70), soup.body or soup)
    lines = []
    for node in area.select('h2,h3,p,li,tr'):
        if node.find_parent('tr') and node.name != 'tr':
            continue
        txt = clean(node.get_text(' ', strip=True))
        if len(txt) >= 5 and txt not in lines:
            lines.append(txt)
    if not lines:
        lines = [clean(area.get_text(' ', strip=True))]
    if not any(len(x) > 20 for x in lines):
        meta = soup.select_one('meta[name="description"]')
        lines = [clean(meta.get('content', ''))] if meta else []
    if not lines or not ''.join(lines).strip():
        raise RuntimeError('Article body not extractable')
    # Extractive detail summary: no invented facts; keep lead + date/price/ticket conditions.
    chosen = lines[:4]
    for line in lines[4:]:
        if DETAIL.search(line) and line not in chosen:
            chosen.append(line)
        if sum(map(len, chosen)) >= 2500:
            break
    summary = '\n'.join(chosen)[:2800]
    return title[:220], summary


DATE_PATTERN = re.compile(
    r'(?<!\d)(20\d{2})\s*[年./\-]\s*(0?[1-9]|1[0-2])\s*[月./\-]\s*'
    r'(0?[1-9]|[12]\d|3[01])\s*日?(?!\d)'
)


def parse_publication_value(value):
    """Parse explicit publication-date text; assume JST if timezone is omitted."""
    if not value:
        return None
    raw = clean(str(value))
    try:
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        match = DATE_PATTERN.search(raw)
        if not match:
            return None
        try:
            dt = datetime(*map(int, match.groups()))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(timezone.utc)


def publication_date(url):
    """Best-effort original publication date; never substitute crawl or update time."""
    soup = fetch(url)
    values = []
    for selector in (
        'meta[property="article:published_time"]', 'meta[name="pubdate"]',
        'meta[name="date"]', 'meta[itemprop="datePublished"]',
        'meta[property="datePublished"]', 'meta[name="datePublished"]',
        'time[datetime]', '[itemprop="datePublished"][datetime]',
        '.news-date', '.news_date', '.entry-date', '.post-date',
        '.publish-date', '.published', '.article-date', '.date',
    ):
        for node in soup.select(selector)[:5]:
            val = node.get('content') or node.get('datetime') or node.get_text(' ', strip=True)
            if val:
                values.append(val)
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or node.get_text())
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            obj = stack.pop()
            if isinstance(obj, list):
                stack.extend(obj)
            elif isinstance(obj, dict):
                if obj.get('datePublished'):
                    values.append(obj['datePublished'])
                stack.extend(v for v in obj.values() if isinstance(v, (dict, list)))
    # Read only date-like elements near the article heading; do not scrape event dates
    # from the article body and mistake them for publication dates.
    for heading in soup.select('h1')[:2]:
        for node in list(heading.previous_elements)[:12] + list(heading.next_elements)[:12]:
            if getattr(node, 'name', None) in ('time', 'span', 'small'):
                cls = ' '.join(node.get('class', [])).lower()
                if 'date' in cls or 'time' in cls or 'publish' in cls:
                    values.append(node.get_text(' ', strip=True))
    # Some official URLs encode YYYYMMDD.
    m = re.search(r'/(20\d{2})(\d{2})(\d{2})(?:/|$)', url)
    if m:
        values.append('-'.join(m.groups()))
    for value in values:
        date = parse_publication_value(value)
        if date:
            return date
    return None


def date_from_url(url):
    path = urlparse(url).path
    patterns = (
        r'(?<!\d)(20\d{2})[/-](0?[1-9]|1[0-2])[/-](0?[1-9]|[12]\d|3[01])(?!\d)',
        r'(?<!\d)(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)',
    )
    for pattern in patterns:
        m = re.search(pattern, path)
        if m:
            try:
                return datetime(*(int(x) for x in m.groups()), tzinfo=JST).astimezone(timezone.utc)
            except ValueError:
                pass
    return None


def publication_status(date):
    if date is None:
        return 'unknown'
    delta = datetime.now(timezone.utc) - date
    if delta < timedelta(hours=-2):
        return 'future'
    return 'recent' if delta <= timedelta(days=MAX_AGE_DAYS) else 'old'


def quota_available(state):
    today = datetime.now(JST).date().isoformat()
    daily = state.setdefault('daily_posts', {})
    # JST calendar day. Remove older counters to avoid unbounded state.
    for key in list(daily):
        if key != today:
            del daily[key]
    return daily.get(today, 0) < MAX_POSTS_PER_DAY


def count_post(state):
    today = datetime.now(JST).date().isoformat()
    daily = state.setdefault('daily_posts', {})
    daily[today] = daily.get(today, 0) + 1
    state['_posts_this_run'] += 1


def can_post(state):
    return state['_posts_this_run'] < MAX_POSTS_PER_RUN and quota_available(state)


def relevant(title, summary, source):
    if not source.get('filter_related'):
        return True
    combined = (title + '\n' + summary).lower()
    if not any(k in combined for k in RELATED):
        return False
    # Exclude only explicitly personal-only stories without a band/project context.
    if any(k in combined for k in PERSONAL) and not any(k in title.lower() for k in RELATED):
        return False
    return True


def post(webhook, content):
    for i in range(5):
        r = SESSION.post(webhook, json={'content': content, 'allowed_mentions': {'parse': []}}, timeout=TIMEOUT)
        if r.status_code == 429:
            try:
                delay = min(max(float(r.json().get('retry_after', 2)), 1), 30)
            except (ValueError, TypeError):
                delay = 3
            time.sleep(delay)
            continue
        if r.status_code >= 500 and i < 4:
            time.sleep(i + 1)
            continue
        r.raise_for_status()
        return
    raise RuntimeError('Discord webhook rate limited')


def send_article(webhook, source, title, summary, url, update=False):
    header = f"**【{'記事更新' if update else '新着'}／{source}】{title}**\n"
    footer = f'\n\n記事URL：{url}'
    # One message per article: avoiding partial multi-message duplicates if a later request fails.
    budget = max(100, 1900 - len(header) - len(footer))
    text = summary if len(summary) <= budget else summary[:budget - 12] + '\n（以下省略）'
    post(webhook, header + text + footer)


def run():
    """Detect new URLs; prefer publication date and cautiously handle undated pages."""
    webhook = os.getenv('BANGDREAM_DISCORD_WEBHOOK_URL', '').strip()
    if not webhook:
        raise RuntimeError('Set BANGDREAM_DISCORD_WEBHOOK_URL in GitHub Secrets')
    sources = json.loads(SOURCES.read_text(encoding='utf-8'))
    state = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {'sources': {}, 'global_sent': {}}
    state.setdefault('sources', {})
    state.setdefault('global_sent', {})
    state.setdefault('pending_v07', {})
    # New detection mode intentionally does NOT backfill older v0.5/v0.6 candidates.
    # A one-time baseline records all currently visible URLs as observed.
    migration = not state.get('url_watch_v07_initialized', False)
    stats = {'discovered': 0, 'new_urls': 0, 'queued': 0, 'sent': 0,
             'filtered': 0, 'deferred': 0, 'baseline': 0, 'errors': 0,
             'old': 0, 'undated_skipped': 0, 'undated_sent': 0, 'recent': 0}
    errors = []
    state['_posts_this_run'] = 0
    enabled = {s['name']: s for s in sources if s.get('enabled', False)}
    all_scanned = True
    for source in sources:
        if not source.get('enabled', False):
            continue
        name = source['name']
        previous = state['sources'].setdefault(name, {'initialized': False, 'seen': {}})
        seen = previous.setdefault('seen', {})
        # Separate URL observation history from v0.6's hash / date states.
        observed = previous.setdefault('observed_v07', {})
        try:
            links = discover(source)
            stats['discovered'] += len(links)
            LOG.info('%s: %d links', name, len(links))
            initial = not previous.get('url_watch_v07_initialized', False)
            for position, link in enumerate(links):
                if link not in observed:
                    observed[link] = now()
                    if initial:
                        stats['baseline'] += 1
                    elif link not in state['global_sent'] and link not in state['pending_v07']:
                        state['pending_v07'][link] = {'source': name, 'first_seen': now(), 'listing_position': position, 'first_discovery': True}
                        stats['new_urls'] += 1
                        LOG.info('New URL queued: %s', link)
                seen.setdefault(link, {'hash': None, 'first_seen': now()})
            # One-time controlled backfill: examine only top listing URLs, never resend.
            # Do not mark complete until a successful listing has been fetched.
            if not previous.get('recent_backfill_v11_done'):
                added = 0
                for position, link in enumerate(links[:BACKFILL_SCAN_PER_SOURCE]):
                    if link not in state['global_sent'] and link not in state['pending_v07']:
                        if date_from_url(link) and publication_status(date_from_url(link)) != 'recent':
                            continue
                        state['pending_v07'][link] = {
                            'source': name, 'first_seen': now(),
                            'listing_position': position, 'first_discovery': False,
                            'backfill_v11': True,
                        }
                        added += 1
                previous['recent_backfill_v11_done'] = True
                LOG.info('%s: recent backfill candidates=%d (date will be verified)', name, added)
            previous['url_watch_v07_initialized'] = True
            previous['initialized'] = True
            if len(observed) > MAX_SEEN:
                previous['observed_v07'] = dict(list(observed.items())[-MAX_SEEN:])
            if len(seen) > MAX_SEEN:
                previous['seen'] = dict(list(seen.items())[-MAX_SEEN:])
        except Exception as exc:
            all_scanned = False
            errors.append(f'{name}: listing: {exc}')
            LOG.error('%s: listing failed: %s', name, exc)
    if migration:
        state['url_watch_v07_initialized'] = True
        LOG.info('URL-based monitoring baseline established; no historical articles sent')
    # A pending URL remains pending until successful Discord delivery or filtering.
    # Work through the queue oldest-first; do not let quota or one failing URL erase it.
    for link, item in sorted(list(state['pending_v07'].items()), key=lambda pair: pair[1].get('first_seen', '')):
        if link in state['global_sent']:
            del state['pending_v07'][link]
            continue
        if not can_post(state):
            stats['deferred'] += 1
            continue
        name = item.get('source', '')
        source = enabled.get(name)
        if source is None:
            # Keep pending in case the source is enabled again.
            continue
        try:
            url_date = date_from_url(link)
            if url_date and publication_status(url_date) in ('old', 'future'):
                stats['old'] += 1
                LOG.info('Skipped dated URL: %s', link)
                del state['pending_v07'][link]
                continue
            published = publication_date(link)
            status = publication_status(published)
            if status in ('old', 'future'):
                stats['old'] += 1
                LOG.info('Skipped %s published=%s: %s', status, published, link)
                del state['pending_v07'][link]
                continue
            if status == 'unknown':
                # An old page first encountered deep in a listing is NOT trustworthy
                # evidence of a new publication. Only genuinely newly observed top links
                # qualify, and never the v0.7 migration baseline.
                if (source.get('discovery') == 'search_rss' or item.get('backfill_v11') or not item.get('first_discovery') or item.get('listing_position', 9999) >= UNKNOWN_DATE_TOP_N):
                    stats['undated_skipped'] += 1
                    LOG.info('Undated, insufficient evidence; skipped: %s', link)
                    del state['pending_v07'][link]
                    continue
                LOG.info('Undated, new top-of-list URL (provisional): %s', link)
            title, summary = extract(link)
            if not relevant(title, summary, source):
                stats['filtered'] += 1
                del state['pending_v07'][link]
                continue
            digest = hashlib.sha256((title + '\n' + summary).encode()).hexdigest()
            send_article(webhook, name, title, summary, link)
            state['global_sent'][link] = digest
            seen = state['sources'][name].setdefault('seen', {})
            seen[link] = {'hash': digest, 'first_seen': item.get('first_seen', now())}
            count_post(state)
            stats['sent'] += 1
            stats['undated_sent' if status == 'unknown' else 'recent'] += 1
            del state['pending_v07'][link]
            LOG.info('Sent %s', link)
        except Exception as exc:
            errors.append(f'{name}: article {link}: {exc}')
            LOG.error('Pending article failed (will retry): %s: %s', link, exc)
    stats['queued'] = len(state['pending_v07'])
    stats['errors'] = len(errors)
    LOG.info('Notifications this run: %d/%d; today (JST): %d/%d',
             state['_posts_this_run'], MAX_POSTS_PER_RUN,
             state.get('daily_posts', {}).get(datetime.now(JST).date().isoformat(), 0), MAX_POSTS_PER_DAY)
    LOG.info('Diagnostics: %s', json.dumps(stats, ensure_ascii=False))
    del state['_posts_this_run']
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if errors:
        LOG.warning('%d source/article errors; other sources continue. State saved.', len(errors))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    run()
