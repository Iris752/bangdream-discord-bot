"""Multi-source BanG Dream official news watcher. No generative AI or paid APIs."""
from __future__ import annotations
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag, parse_qsl, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'state.json'
SOURCES = ROOT / 'sources.json'
LOG = logging.getLogger('bangdream')
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; BangDreamPersonalNewsWatcher/2.0)'})
TIMEOUT = 20
MAX_PER_SOURCE = 25
MAX_POSTS_PER_RUN = 5
MAX_POSTS_PER_DAY = 24
MAX_ARTICLE_AGE_DAYS = 14
MAX_SEEN = 12000
JST = ZoneInfo("Asia/Tokyo")
BACKFILL_START = datetime(2026, 10, 20, tzinfo=JST).astimezone(timezone.utc)
BACKFILL_KEY = "backfill_since_2026_10_20_v1"
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
    return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/') + '/', '', urlencode(qs), ''))


def fetch(url):
    for i in range(3):
        try:
            r = SESSION.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            if len(r.content) > 5_000_000:
                raise ValueError('Page too large')
            return BeautifulSoup(r.content, 'lxml')
        except (requests.RequestException, ValueError):
            if i == 2:
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


def discover(source):
    found = []
    visited = set()
    # Listing pagination must be configured explicitly; no guessing arbitrary URLs.
    for listing in [source['url']] + source.get('additional_list_pages', []):
        soup = fetch(listing)
        for a in soup.select('a[href]'):
            u = canonical(urljoin(listing, a['href']))
            if u not in visited and article_link(u, source):
                visited.add(u)
                found.append(u)
        time.sleep(0.3)
    if not found:
        raise RuntimeError('No article URLs discovered; check selector, page and article_pattern')
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
    """Use article-specific metadata and publication labels, not event dates."""
    soup = fetch(url)
    values = []
    selectors = (
        'meta[property="article:published_time"]',
        'meta[name="date"]', 'meta[name="pubdate"]',
        'meta[itemprop="datePublished"]', 'meta[property="datePublished"]',
        'meta[name="datePublished"]', 'meta[name="DC.date.issued"]',
        'meta[name="dc.date"]', 'time[datetime]',
        'time[pubdate]', '[itemprop="datePublished"]',
    )
    for selector in selectors:
        for node in soup.select(selector):
            value = node.get('content') or node.get('datetime') or node.get_text(' ', strip=True)
            if value:
                values.append((selector, value))
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or node.get_text())
            queue = [data]
            while queue:
                item = queue.pop()
                if isinstance(item, list):
                    queue.extend(item)
                elif isinstance(item, dict):
                    if item.get('datePublished'):
                        values.append(('jsonld.datePublished', item['datePublished']))
                    queue.extend(v for v in item.values() if isinstance(v, (dict, list)))
        except (ValueError, TypeError):
            continue
    # Date shown near the article heading, not dates of concerts/ticket sales in body.
    for selector in (
        'article .date', 'article .news-date', 'article .news_date',
        'article .post-date', 'article .published', 'article time',
        '.news-detail .date', '.news_detail .date',
        '.newsDetail .date', '.newsDetail__date', '.news-detail__date',
        '.news-date', '.news_date', '.post-date', '.publish-date',
        '.published-date', '.entry-date', '.article-date',
        '.news__date', '.news-detail__head time',
    ):
        for node in soup.select(selector)[:5]:
            value = node.get('datetime') or node.get('content') or node.get_text(' ', strip=True)
            if value:
                values.append((selector, value))
    # Date-bearing URL path is used only when it is unambiguously YYYYMMDD.
    match = re.search(r'/(20\d{2})(\d{2})(\d{2})(?:/|$)', urlparse(url).path)
    if match:
        values.append(('url', '-'.join(match.groups())))
    for origin, value in values:
        dt = parse_publication_value(value)
        if dt:
            LOG.debug('Publication date via %s: %s => %s', origin, url, dt.isoformat())
            return dt
    LOG.info('No publication date in supported fields: %s', url)
    return None


def in_window(date):
    return date is not None and date >= BACKFILL_START and timedelta(0) <= datetime.now(timezone.utc) - date <= timedelta(days=MAX_ARTICLE_AGE_DAYS)


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
    webhook = os.getenv('BANGDREAM_DISCORD_WEBHOOK_URL', '').strip()
    if not webhook:
        raise RuntimeError('Set BANGDREAM_DISCORD_WEBHOOK_URL in GitHub Secrets')
    sources = json.loads(SOURCES.read_text(encoding='utf-8'))
    state = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {'sources': {}, 'global_sent': {}}
    state.setdefault('sources', {})
    state.setdefault('global_sent', {})
    # Old global URL entries remain valid after upgrading from v02.
    # A one-time backfill rechecks baseline-only URLs without resetting the state.
    # Only a successful Discord post counts as sent.
    backfill = not state.get(BACKFILL_KEY, False)
    backfill_remaining = False
    stats = {'new_urls': 0, 'backfill_candidates': 0, 'sent': 0, 'already_sent': 0, 'old': 0, 'undated': 0, 'deferred': 0, 'filtered': 0}
    errors = []
    state['_posts_this_run'] = 0
    for source in sources:
        if not source.get('enabled', False):
            continue
        name = source['name']
        previous = state['sources'].get(name, {'initialized': False, 'seen': {}})
        seen = previous.setdefault('seen', {})
        try:
            links = discover(source)
            LOG.info('%s: %d links', name, len(links))
            if not previous.get('initialized'):
                # New sources are baselined without mass posting; existing source
                # baselines can be rechecked during the one-time 10/20 backfill.
                for link in links:
                    seen.setdefault(link, {'hash': None, 'first_seen': now()})
                previous['initialized'] = True
                LOG.info('%s: baseline created (%d)', name, len(links))
            else:
                pending = [link for link in links if link not in seen or (seen[link].get('hash') is None and link not in state['global_sent'])]
                stats['new_urls'] += len(pending)
                baseline_only = [link for link in links if link in seen and seen[link].get('hash') in (None, 'out_of_window') and link not in state['global_sent']]
                candidates = list(dict.fromkeys((baseline_only if backfill else []) + pending))
                stats['backfill_candidates'] += len(baseline_only) if backfill else 0
                # Listing order is generally newest first; process in that order.
                for link in candidates[:MAX_PER_SOURCE]:
                    try:
                        if link in state['global_sent']:
                            seen.setdefault(link, {'first_seen': now()})['hash'] = state['global_sent'][link]
                            stats['already_sent'] += 1
                            continue
                        published = publication_date(link)
                        if published is None:
                            stats['undated'] += 1
                            # Do not mark as sent or discard; retry on future runs.
                            LOG.info('Date unavailable (retry later): %s', link)
                            continue
                        if not in_window(published):
                            stats['old'] += 1
                            seen.setdefault(link, {'first_seen': now()})['hash'] = 'out_of_window'
                            continue
                        if not can_post(state):
                            stats['deferred'] += 1
                            backfill_remaining = True
                            LOG.info('Notification quota reached; defer %s', link)
                            continue
                        title, summary = extract(link)
                        digest = hashlib.sha256((title + '\n' + summary).encode()).hexdigest()
                        if not relevant(title, summary, source):
                            seen.setdefault(link, {'first_seen': now()})['hash'] = 'filtered'
                            stats['filtered'] += 1
                            continue
                        send_article(webhook, name, title, summary, link)
                        state['global_sent'][link] = digest
                        seen[link] = {'hash': digest, 'first_seen': now()}
                        count_post(state)
                        stats['sent'] += 1
                        LOG.info('Sent %s', link)
                    except Exception as exc:
                        backfill_remaining = True
                        errors.append(f'{name}: article {link}: {exc}')
                if len(candidates) > MAX_PER_SOURCE:
                    backfill_remaining = True
                # Update detection only for articles actually sent before.
                if source.get('detect_updates', True):
                    for link in links[:10]:
                        old = seen.get(link, {})
                        if link not in state['global_sent'] or not old.get('hash') or old['hash'] in ('filtered', 'out_of_window'):
                            continue
                        try:
                            if not in_window(publication_date(link)) or not can_post(state):
                                continue
                            title, summary = extract(link)
                            digest = hashlib.sha256((title + '\n' + summary).encode()).hexdigest()
                            if digest != state['global_sent'][link] and relevant(title, summary, source):
                                send_article(webhook, name, title, summary, link, update=True)
                                count_post(state)
                                stats['sent'] += 1
                                old['hash'] = digest
                                state['global_sent'][link] = digest
                        except Exception as exc:
                            errors.append(f'{name}: update {link}: {exc}')
                        time.sleep(0.3)
            if len(seen) > MAX_SEEN:
                previous['seen'] = dict(list(seen.items())[-MAX_SEEN:])
            state['sources'][name] = previous
        except Exception as exc:
            errors.append(f'{name}: listing: {exc}')
    LOG.info('Notifications this run: %d/%d; today (JST): %d/%d', state['_posts_this_run'], MAX_POSTS_PER_RUN, state.get('daily_posts', {}).get(datetime.now(JST).date().isoformat(), 0), MAX_POSTS_PER_DAY)
    LOG.info('Diagnostics: %s', json.dumps(stats, ensure_ascii=False))
    if backfill and not backfill_remaining and not errors:
        state[BACKFILL_KEY] = True
        LOG.info('Backfill complete; one-time recheck disabled')
    elif backfill:
        LOG.info('Backfill remains active for deferred/error items')
    del state['_posts_this_run']
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if errors:
        for error in errors:
            LOG.error(error)
        raise RuntimeError(f'{len(errors)} source/article errors; see logs. Successful state saved.')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    run()