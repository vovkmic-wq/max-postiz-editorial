"""Нормализация серверных источников без анонимного доступа и публикации."""
import copy
import math
import re
import unicodedata
from collections import Counter
from urllib.parse import urlsplit

from .collectors import detect_language
from .config import AgentError, fingerprint
from .reddit import text_hash


REDDIT_DEFAULTS = {
    'subreddits': [], 'authors': [], 'keywords': [], 'min_score': 0,
    'min_comments': 0, 'max_age_hours': 72, 'limit': 25,
    'sort': 'top', 'time_filter': 'week', 'max_pages': 3,
}
DEFAULT_REDDIT_SETTINGS = REDDIT_DEFAULTS
_BOUNDS = {'min_score': (0, 1_000_000), 'min_comments': (0, 1_000_000),
           'max_age_hours': (1, 8760), 'limit': (1, 100), 'max_pages': (1, 5)}
REDDIT_SETTINGS_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': list(REDDIT_DEFAULTS),
    'properties': {
        **{key: {'type': 'array', 'maxItems': 30 if key == 'keywords' else 20,
                 'items': {'type': 'string', 'minLength': 1,
                           'maxLength': 120 if key == 'keywords' else 35}}
           for key in ('subreddits', 'authors', 'keywords')},
        **{key: {'type': 'integer', 'minimum': low, 'maximum': high}
           for key, (low, high) in _BOUNDS.items()},
        'sort': {'type': 'string', 'enum': ['top', 'new', 'hot']},
        'time_filter': {'type': 'string', 'enum': ['hour', 'day', 'week', 'month', 'year', 'all']},
    },
}
_NAME = re.compile(r'[A-Za-z0-9_\-]{1,32}')
_ID = re.compile(r't3_[a-z0-9]{1,16}')
_HASH = re.compile(r'[0-9a-f]{64}')
_POLITICAL_SUBS = {'politics', 'worldnews', 'news', 'geopolitics', 'ukpolitics',
                   'politicaldiscussion', 'politicalhumor', 'conservative',
                   'democrats', 'republican', 'republicans', 'libertarian', 'neutralpolitics'}
_POLITICS = re.compile(
    r'\b(?:politic\w*|election\w*|parliament\w*|congress\w*|senat(?:e|or)\w*|'
    r'democrat\w*|republican\w*|geopolitic\w*|trump|biden|putin|zelensky\w*)\b', re.I)
_BAD_FLAGS = ('over_18', 'spoiler', 'stickied', 'promoted', 'is_ad',
              'is_created_from_ads_ui', 'quarantine')
_DELETED = {'', '[deleted]', '[removed]'}


def normalize_reddit_settings(value, *, require_scope=True):
    """Возвращает новый JSON; секреты и разрешения в пользовательские поля не входят."""
    if not isinstance(value, dict) or set(value) - set(REDDIT_DEFAULTS):
        raise AgentError('REDDIT_COLLECTION_CONFIG_INVALID')
    result = copy.deepcopy(REDDIT_DEFAULTS)
    result.update(copy.deepcopy(value))
    for key in ('subreddits', 'authors', 'keywords'):
        values = result[key]
        if not isinstance(values, list) or len(values) > (30 if key == 'keywords' else 20):
            raise AgentError('REDDIT_COLLECTION_CONFIG_INVALID')
        normalized = []
        for item in values:
            if not isinstance(item, str) or any(ord(c) < 32 for c in item):
                raise AgentError('REDDIT_COLLECTION_CONFIG_INVALID')
            item = ' '.join(unicodedata.normalize('NFKC', item).split()).casefold()
            if key == 'keywords':
                if not item or len(item) > 120:
                    raise AgentError('REDDIT_COLLECTION_CONFIG_INVALID')
            else:
                prefix = 'r' if key == 'subreddits' else 'u'
                item = re.sub(r'^/?' + prefix + '/', '', item)
                if not _NAME.fullmatch(item):
                    raise AgentError('REDDIT_COLLECTION_CONFIG_INVALID')
            if item not in normalized:
                normalized.append(item)
        result[key] = normalized
    for key, (minimum, maximum) in _BOUNDS.items():
        if type(result[key]) is not int or not minimum <= result[key] <= maximum:
            raise AgentError('REDDIT_COLLECTION_CONFIG_INVALID')
    if (not isinstance(result['sort'], str) or result['sort'] not in {'top', 'new', 'hot'}
            or not isinstance(result['time_filter'], str)
            or result['time_filter'] not in {'hour', 'day', 'week', 'month', 'year', 'all'}):
        raise AgentError('REDDIT_COLLECTION_CONFIG_INVALID')
    if require_scope and not (result['authors'] or result['subreddits']):
        raise AgentError('REDDIT_COLLECTION_SCOPE_REQUIRED', 409)
    return result


def _fullname(value):
    if not isinstance(value, str):
        return None
    value = value.casefold()
    if re.fullmatch(r'[a-z0-9]{1,16}', value):
        value = 't3_' + value
    return value if _ID.fullmatch(value) else None


def canonical_reddit_post_url(value):
    """Удаляет slug и tracking, но не допускает другой host, id или схему."""
    try:
        if not isinstance(value, str) or not value.strip():
            raise ValueError
        value = value.strip()
        if value.startswith('/') and not value.startswith('//'):
            value = 'https://www.reddit.com' + value
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or parsed.hostname not in
                {'reddit.com', 'www.reddit.com', 'old.reddit.com', 'new.reddit.com'}
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise ValueError
        match = re.fullmatch(
            r'/r/([A-Za-z0-9_\-]{1,32})/comments/([a-z0-9]{1,16})(?:/[^?#]*)?', parsed.path)
        if not match:
            raise ValueError
        subreddit, ident = match.groups()
        return f'https://www.reddit.com/r/{subreddit.casefold()}/comments/{ident}/'
    except (ValueError, TypeError) as exc:
        raise AgentError('REDDIT_POST_URL_REQUIRED') from exc


def _identity(post):
    ident = _fullname(post.get('id'))
    name = post.get('name')
    if not ident or (name is not None and name != ident):
        return None
    try:
        url = canonical_reddit_post_url(post.get('permalink'))
    except AgentError:
        return None
    pieces = urlsplit(url).path.split('/')
    if pieces[4] != ident[3:]:
        return None
    subreddit = post.get('subreddit', pieces[2])
    if not isinstance(subreddit, str) or subreddit.casefold() != pieces[2]:
        return None
    # В self-post поле url должно ссылаться на тот же пост, а не на внешний сайт.
    if post.get('url'):
        try:
            destination = canonical_reddit_post_url(post['url'])
            parent = post.get('crosspost_parent')
            destination_id = _fullname(urlsplit(destination).path.split('/')[4])
            if destination != url and not (parent and destination_id == parent):
                return None
        except AgentError:
            return None
    return ident, url, subreddit.casefold()


def _unsafe(post):
    if any(post.get(key, False) is not False for key in _BAD_FLAGS):
        return 'unsafe_flags'
    if (post.get('removed_by_category') or post.get('banned_by')
            or post.get('promoted_by') or post.get('promoted_display_name')
            or post.get('subreddit_type') in {'private', 'gold_restricted'}):
        return 'removed_or_ad'
    title = post.get('title')
    if not isinstance(title, str) or not title.strip():
        return 'invalid_text'
    if _POLITICS.search(title + ' ' + str(post.get('link_flair_text') or '')):
        return 'politics'
    if re.search(r'\b(?:advertisement|sponsored|paid promotion|nsfw)\b',
                 title + ' ' + str(post.get('link_flair_text') or ''), re.I):
        return 'ad_or_nsfw_flair'
    return None


def _original(post):
    aliases, urls, seen = [], [], set()
    for _ in range(4):
        if not isinstance(post, dict):
            return None, 'invalid_post'
        reason = _unsafe(post)
        identity = _identity(post)
        if reason or not identity:
            return None, reason or 'invalid_identity'
        ident, url, _ = identity
        if ident in seen:
            return None, 'crosspost_cycle'
        seen.add(ident)
        aliases.append(ident)
        urls.append(url)
        parent = post.get('crosspost_parent')
        if not parent:
            if post.get('crosspost_parent_list'):
                return None, 'crosspost_parent_missing'
            return (post, identity, aliases, urls), None
        parents = post.get('crosspost_parent_list')
        if (not isinstance(parent, str) or not _ID.fullmatch(parent)
                or not isinstance(parents, list) or len(parents) != 1
                or not isinstance(parents[0], dict)
                or _fullname(parents[0].get('id')) != parent):
            return None, 'crosspost_parent_missing'
        post = parents[0]
    return None, 'crosspost_depth'


def _grants(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 1000:
        raise AgentError('REDDIT_RIGHTS_POLICY_INVALID', 409)
    result = []
    for grant in value:
        if not isinstance(grant, dict):
            raise AgentError('REDDIT_RIGHTS_POLICY_INVALID', 409)
        if set(grant) - {'scope_type', 'scope', 'verified', 'allow_storage', 'allow_derivative',
                         'allow_redistribution', 'evidence_ref', 'source_hash', 'expires_at',
                         'license', 'attribution'}:
            raise AgentError('REDDIT_RIGHTS_POLICY_INVALID', 409)
        if not all(grant.get(key) is True for key in
                   ('verified', 'allow_storage', 'allow_derivative', 'allow_redistribution')):
            continue
        scope, kind = grant.get('scope'), grant.get('scope_type')
        evidence = grant.get('evidence_ref')
        if (not isinstance(kind, str) or kind not in {'post', 'author'}
                or not isinstance(scope, str)
                or not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 2000):
            raise AgentError('REDDIT_RIGHTS_POLICY_INVALID', 409)
        normalized = copy.deepcopy(grant)
        if kind == 'post':
            normalized['scope'] = _fullname(scope)
            if (not normalized['scope'] or not isinstance(grant.get('source_hash'), str)
                    or not _HASH.fullmatch(grant['source_hash'])):
                raise AgentError('REDDIT_RIGHTS_POLICY_INVALID', 409)
        else:
            if not _NAME.fullmatch(scope):
                raise AgentError('REDDIT_RIGHTS_POLICY_INVALID', 409)
            normalized['scope'] = scope.casefold()
        if ('source_hash' in grant and (not isinstance(grant['source_hash'], str)
                                       or not _HASH.fullmatch(grant['source_hash']))):
            raise AgentError('REDDIT_RIGHTS_POLICY_INVALID', 409)
        expires = grant.get('expires_at')
        if expires is not None and (type(expires) not in (int, float)
                                   or not math.isfinite(expires) or expires <= 0):
            raise AgentError('REDDIT_RIGHTS_POLICY_INVALID', 409)
        if any(not isinstance(grant[key], str) or len(grant[key]) > 2000
               for key in ('license', 'attribution') if key in grant):
            raise AgentError('REDDIT_RIGHTS_POLICY_INVALID', 409)
        result.append(normalized)
    return result


def _right_for(grants, ident, author, source_hash, now):
    for grant in grants:
        if grant.get('expires_at') is not None and grant['expires_at'] <= now:
            continue
        if grant.get('source_hash') is not None and grant['source_hash'] != source_hash:
            continue
        if ((grant['scope_type'] == 'author' and grant['scope'] == author)
                or (grant['scope_type'] == 'post' and grant['scope'] == ident
                    and grant['source_hash'] == source_hash)):
            return {**copy.deepcopy(grant), 'grant_hash': fingerprint(grant),
                    'source_hash': source_hash, 'source_post_id': ident, 'source_author': author}
    return None


def _seen(value):
    if value is None:
        return {'ids': set(), 'urls': set(), 'text_hashes': set()}
    if isinstance(value, (set, list, tuple)):
        value = {'ids': value}
    if not isinstance(value, dict) or set(value) - {'ids', 'urls', 'text_hashes'}:
        raise AgentError('REDDIT_DEDUP_STATE_INVALID', 409)
    result = {}
    for key in ('ids', 'urls', 'text_hashes'):
        values = value.get(key, [])
        if not isinstance(values, (list, tuple, set)) or any(not isinstance(v, str) for v in values):
            raise AgentError('REDDIT_DEDUP_STATE_INVALID', 409)
        result[key] = set()
        for item in values:
            if key == 'ids':
                item = _fullname(item)
                if item is None:
                    raise AgentError('REDDIT_DEDUP_STATE_INVALID', 409)
            elif key == 'urls':
                item = canonical_reddit_post_url(item)
            elif not _HASH.fullmatch(item):
                raise AgentError('REDDIT_DEDUP_STATE_INVALID', 409)
            result[key].add(item)
    return result


def collect_reddit_candidates(client, settings, *, now, seen=None, rights_grants=None):
    """Готовит EN-кандидатов; БД, переводчик, права доступа и публикация остаются у caller."""
    settings = normalize_reddit_settings(settings, require_scope=True)
    if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
        raise AgentError('REDDIT_COLLECTION_TIME_INVALID')
    if getattr(client, 'options', {}).get('access_approved') is not True:
        raise AgentError('REDDIT_APPROVAL_REQUIRED', 409)
    grants, prior = _grants(rights_grants), _seen(seen)
    posts = client.collect(settings)
    if not isinstance(posts, list):
        raise AgentError('REDDIT_RESPONSE_INVALID', 503)
    accepted, counts = [], Counter()
    for raw in posts:
        result, reason = _original(raw)
        if reason:
            counts[reason] += 1
            continue
        post, (ident, url, subreddit), ids, urls = result
        author = post.get('author')
        title, body = post.get('title'), post.get('selftext')
        created, score, comments = (post.get('created_utc'), post.get('score'),
                                    post.get('num_comments'))
        if (not isinstance(author, str) or not _NAME.fullmatch(author)
                or author.casefold() in {'deleted', 'removed'} or post.get('is_self') is not True
                or not isinstance(body, str) or body.strip().casefold() in _DELETED
                or len(title) > 500 or len(body) > 8500 or len(title + body) > 9000):
            counts['invalid_or_deleted_text'] += 1
            continue
        author = author.casefold()
        if (type(created) not in (int, float) or not math.isfinite(created)
                or created > now + 300 or created < now - settings['max_age_hours'] * 3600
                or type(score) is not int or type(comments) is not int or comments < 0):
            counts['age_or_metrics'] += 1
            continue
        if ((settings['authors'] and author not in settings['authors'])
                or (settings['subreddits'] and subreddit not in settings['subreddits'])
                or score < settings['min_score'] or comments < settings['min_comments']):
            counts['configured_filters'] += 1
            continue
        text = title + '\n\n' + body
        if subreddit in _POLITICAL_SUBS or _POLITICS.search(text):
            counts['politics'] += 1
            continue
        if detect_language(text) != 'en':
            counts['language'] += 1
            continue
        haystack = ' '.join(unicodedata.normalize('NFKC', text).casefold().split())
        if settings['keywords'] and not any(re.search(r'(?<!\w)' + re.escape(word) + r'(?!\w)',
                                                     haystack) for word in settings['keywords']):
            counts['keywords'] += 1
            continue
        hashes = sorted({text_hash(body), text_hash(text)})
        rights = _right_for(grants, ident, author, text_hash(text), now)
        if rights is None:
            counts['rights_required'] += 1
            continue
        keys = {'ids': sorted(set(ids)), 'urls': sorted(set(urls)), 'text_hashes': hashes}
        if any(prior[key].intersection(values) for key, values in keys.items()):
            counts['duplicate'] += 1
            continue
        accepted.append({'article': {'url': url, 'title': title, 'original': body,
                                     'published_at': created},
                         'source_id': 'reddit_oauth', 'external_id': ident,
                         'author': author, 'subreddit': subreddit, 'score': score,
                         'num_comments': comments, 'rights': rights, 'dedup_keys': keys})
    # Сначала ранжирование, затем дедуп внутри выдачи: выбираем лучший вариант,
    # а не первый встретившийся дубль с меньшим рейтингом.
    if settings['sort'] == 'new':
        accepted.sort(key=lambda x: (-x['article']['published_at'], -x['score'], x['external_id']))
    else:
        accepted.sort(key=lambda x: (-x['score'], -x['num_comments'],
                                     -x['article']['published_at'], x['external_id']))
    output = []
    local = {key: set() for key in prior}
    for item in accepted:
        if any(local[key].intersection(values) for key, values in item['dedup_keys'].items()):
            counts['duplicate'] += 1
            continue
        for key, values in item['dedup_keys'].items():
            local[key].update(values)
        if len(output) < settings['limit']:
            output.append(item)
    return {'candidates': output,
            'diagnostics': {'scanned': len(posts), 'accepted': len(output),
                            'excluded': dict(counts),
                            'listing': copy.deepcopy(getattr(client, 'collection_info', {})),
                            'semantic_review_still_required': True,
                            'rights_grants_from_trusted_policy_only': True}}
