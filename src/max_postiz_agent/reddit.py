"""Approved OAuth collection and local translation checks; no anonymous fallback."""
import hashlib
import json
import math
import os
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from .adapters import extract_object
from .collectors import detect_language
from .config import AgentError
from .editorial import translation_gate


def text_hash(text):
    return hashlib.sha256(' '.join(text.casefold().split()).encode()).hexdigest()


def slot_at(timestamp):
    local = datetime.fromtimestamp(timestamp, ZoneInfo('Europe/Moscow'))
    return local.replace(hour=local.hour // 4 * 4, minute=0, second=0, microsecond=0)


class RedditClient:
    def __init__(self, options, transport=None):
        self.options = options
        self.http = httpx.Client(timeout=30, trust_env=False, follow_redirects=False,
                                 transport=transport)
        self.token = None
        self.remaining = None
        self.token_until = None
        self.rate_until = None
        self.collection_info = {}

    def _response(self, response):
        if response.status_code == 429:
            raise AgentError('REDDIT_RATE_LIMIT', 429)
        if response.status_code in {401, 403}:
            raise AgentError('REDDIT_ACCESS_DENIED', 403)
        if response.is_redirect or response.status_code >= 400:
            raise AgentError('REDDIT_UNAVAILABLE', 503)
        try:
            value = response.json()
        except ValueError as exc:
            raise AgentError('REDDIT_RESPONSE_INVALID', 503) from exc
        if not isinstance(value, dict):
            raise AgentError('REDDIT_RESPONSE_INVALID', 503)
        if response.headers.get('x-ratelimit-remaining') is not None:
            try:
                self.remaining = float(response.headers['x-ratelimit-remaining'])
                if not math.isfinite(self.remaining) or self.remaining < 0:
                    raise ValueError
                reset = response.headers.get('x-ratelimit-reset')
                if reset is not None:
                    reset = float(reset)
                    if not math.isfinite(reset) or not 0 <= reset <= 86400:
                        raise ValueError
                    self.rate_until = time.monotonic() + reset
            except ValueError:
                raise AgentError('REDDIT_RESPONSE_INVALID', 503) from None
        if value.get('error') in (401, 403):
            raise AgentError('REDDIT_ACCESS_DENIED', 403)
        if value.get('error') == 429:
            raise AgentError('REDDIT_RATE_LIMIT', 429)
        return value

    def request(self, path, params=None):
        if self.options.get('access_approved') is not True:
            raise AgentError('REDDIT_APPROVAL_REQUIRED', 409)
        names = ('client_id', 'client_secret', 'refresh_token', 'user_agent')
        envs = {name: self.options.get(name + '_env') for name in names}
        if any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', v)
               for v in envs.values()):
            raise AgentError('REDDIT_OAUTH_REQUIRED', 409)
        values = {name: os.getenv(envs[name], '') for name in names}
        if not all(values.values()):
            raise AgentError('REDDIT_OAUTH_REQUIRED', 409)
        if not isinstance(path, str) or not re.fullmatch(r'/[A-Za-z0-9_/,\-]+', path):
            raise AgentError('REDDIT_PATH_INVALID')
        if self.rate_until is not None and time.monotonic() >= self.rate_until:
            self.remaining, self.rate_until = None, None
        if self.remaining is not None and self.remaining < 1:
            raise AgentError('REDDIT_RATE_LIMIT', 429)
        try:
            if self.token_until is not None and time.monotonic() >= self.token_until:
                self.token, self.token_until = None, None
            if self.token is None:
                result = self._response(self.http.post('https://www.reddit.com/api/v1/access_token',
                    auth=(values['client_id'], values['client_secret']),
                    headers={'User-Agent': values['user_agent']},
                    data={'grant_type': 'refresh_token', 'refresh_token': values['refresh_token']}))
                self.token = result.get('access_token')
                if (not isinstance(self.token, str) or not 1 <= len(self.token) <= 4096
                        or not re.fullmatch(r'[A-Za-z0-9._~+/\-=]+', self.token)
                        or (result.get('token_type') is not None
                            and (not isinstance(result['token_type'], str)
                                 or result['token_type'].casefold() != 'bearer'))):
                    self.token = None
                    raise AgentError('REDDIT_OAUTH_REJECTED', 403)
                expires = result.get('expires_in')
                if expires is not None:
                    if (type(expires) not in (int, float) or not math.isfinite(expires)
                            or not 1 <= expires <= 86400):
                        self.token = None
                        raise AgentError('REDDIT_OAUTH_REJECTED', 403)
                    self.token_until = time.monotonic() + expires - min(30, expires / 2)
            if self.remaining is not None and self.remaining < 1:
                raise AgentError('REDDIT_RATE_LIMIT', 429)
            return self._response(self.http.get('https://oauth.reddit.com' + path,
                headers={'Authorization': 'Bearer ' + self.token, 'User-Agent': values['user_agent']},
                params=params))
        except httpx.HTTPError as exc:
            raise AgentError('REDDIT_UNAVAILABLE', 503) from exc

    def candidates(self):
        listing = self.options['listing']
        parsed = urlsplit(listing)
        if (parsed.scheme, parsed.netloc, parsed.path) != ('https', 'oauth.reddit.com', '/r/popular/top'):
            raise AgentError('REDDIT_LISTING_INVALID')
        limit = min(500, max(1, self.options.get('max_candidates', 500)))
        collected, after, seen_pages = [], None, set()
        while len(collected) < limit:
            params = {'t': 'day', 'limit': min(100, limit - len(collected)), 'raw_json': 1}
            if after:
                params['after'] = after
            value = self.request(parsed.path, params)
            try:
                data = value['data']
                children = data['children']
                if not isinstance(children, list):
                    raise TypeError
                collected.extend(x['data'] for x in children if x.get('kind') == 't3')
                after = data.get('after')
            except (KeyError, TypeError) as exc:
                raise AgentError('REDDIT_RESPONSE_INVALID', 503) from exc
            if not children or not after:
                break
            if after in seen_pages:
                raise AgentError('REDDIT_PAGINATION_INVALID', 503)
            seen_pages.add(after)
        return sorted(collected[:limit], key=lambda x: (-x.get('score', 0), -x.get('num_comments', 0)))

    def collect(self, settings):
        """Читает только разрешённые OAuth-ленты; ошибки доступа не повторяются."""
        from .server_sources import normalize_reddit_settings

        settings = normalize_reddit_settings(settings, require_scope=True)
        if self.options.get('access_approved') is not True:
            raise AgentError('REDDIT_APPROVAL_REQUIRED', 409)
        if settings['authors']:
            feeds = [(f'/user/{name}/submitted', {'sort': settings['sort']})
                     for name in settings['authors']]
        else:
            feeds = [(f'/r/{name}/{settings["sort"]}', {})
                     for name in settings['subreddits']]
        # По одному запросу каждой ленте прежде следующей страницы: одна лента
        # не должна исчерпать общий бюджет и скрыть остальных авторов.
        pending = deque((path, params, None, 0) for path, params in feeds)
        cursors, posts, pages = {}, [], 0
        budget = 20
        while pending and pages < budget:
            path, extras, after, count = pending.popleft()
            params = {'limit': 100, 'raw_json': 1, **extras}
            if settings['sort'] == 'top':
                params['t'] = settings['time_filter']
            if after:
                params['after'] = after
            value = self.request(path, params)
            pages += 1
            try:
                data = value['data']
                children, following = data['children'], data.get('after')
                if (not isinstance(children, list) or len(children) > 100
                        or (following is not None and (not isinstance(following, str)
                            or not re.fullmatch(r't3_[a-z0-9]{1,16}', following)))):
                    raise ValueError
                for child in children:
                    if not isinstance(child, dict):
                        raise ValueError
                    if child.get('kind') == 't3':
                        if not isinstance(child.get('data'), dict):
                            raise ValueError
                        posts.append(child['data'])
            except (KeyError, TypeError, ValueError) as exc:
                raise AgentError('REDDIT_RESPONSE_INVALID', 503) from exc
            if following:
                seen = cursors.setdefault(path, set())
                if following in seen:
                    raise AgentError('REDDIT_PAGINATION_INVALID', 503)
                seen.add(following)
            if children and following and count + 1 < settings['max_pages']:
                pending.append((path, extras, following, count + 1))
        self.collection_info = {'pages': pages, 'feeds': len(feeds),
                                'request_budget': budget, 'budget_exhausted': bool(pending),
                                'raw_posts': len(posts), 'max_pages_per_feed': settings['max_pages']}
        return posts

    def close(self):
        self.http.close()


def eligible(post):
    """Conservative text-only shortlist. The independent reviewer checks the topic."""
    if (post.get('over_18') or post.get('spoiler') or post.get('stickied')
            or post.get('promoted') or post.get('removed_by_category') or not post.get('is_self')):
        return False
    title, body = post.get('title', ''), post.get('selftext', '')
    if not isinstance(title, str) or not isinstance(body, str):
        return False
    if body.strip() in {'', '[deleted]', '[removed]'} or len(title + body) > 9000:
        return False
    if detect_language(title + ' ' + body) != 'en':
        return False
    return bool(re.fullmatch(r'[a-z0-9]+', post.get('id', ''))
                and re.fullmatch(r'/r/[A-Za-z0-9_]+/comments/[a-z0-9]+/[^?#]*', post.get('permalink', '')))


class RedditQuality:
    def __init__(self, translator, reviewer, spelling_url='http://127.0.0.1:8010/v2/check', transport=None, prompt_dir=None):
        self.translator, self.reviewer = translator, reviewer
        self.spelling = httpx.Client(timeout=60, trust_env=False, transport=transport)
        if spelling_url != 'http://127.0.0.1:8010/v2/check':
            raise AgentError('LOCAL_SPELLING_REQUIRED')
        self.spelling_url = spelling_url
        prompts = Path(prompt_dir) if prompt_dir else Path(__file__).resolve().parents[2] / 'prompts'
        self.translation_prompt = (prompts / 'REDDIT_TRANSLATE.txt').read_text(encoding='utf-8')
        self.review_prompt = (prompts / 'REDDIT_REVIEW.txt').read_text(encoding='utf-8')

    def prepare(self, post):
        if not eligible(post):
            raise AgentError('REDDIT_CANDIDATE_EXCLUDED')
        original = post['title'] + '\n\n' + post['selftext']
        context = {'original': original, 'instruction': self.translation_prompt}
        for _ in range(3):
            try:
                translated = extract_object(self.translator.generate_text(json.dumps(context, ensure_ascii=False))[0])
            except AgentError as exc:
                if exc.code != 'INVALID_EDITOR_RESULT':
                    raise
                context['format_error'] = 'Return a single valid JSON object with title and body string fields. No other text.'
                continue
            if set(translated) != {'title', 'body'} or not all(isinstance(v, str) and v.strip() for v in translated.values()):
                raise AgentError('REDDIT_TRANSLATION_INVALID')
            text = translated['title'] + '\n\n' + translated['body']
            translation_gate(original, text)
            check_prompt = {'original_untrusted': original, 'translation_untrusted': text,
                'instruction': self.review_prompt}
            if context.get('review_format_error'):
                check_prompt['format_hint'] = context['review_format_error']
            try:
                reviewed = extract_object(self.reviewer.generate_text(json.dumps(check_prompt, ensure_ascii=False))[0])
            except AgentError as exc:
                if exc.code != 'INVALID_EDITOR_RESULT':
                    raise
                context['review_format_error'] = 'Independent review was not valid JSON; do not treat this as approval.'
                continue
            if (set(reviewed) != {'approved', 'topic', 'issues'} or type(reviewed['approved']) is not bool
                    or not isinstance(reviewed['issues'], list)
                    or any(not isinstance(x, str) for x in reviewed['issues'])):
                raise AgentError('REDDIT_REVIEW_INVALID')
            try:
                response = self.spelling.post(self.spelling_url, data={'language': 'ru-RU', 'text': text})
                response.raise_for_status()
                matches = response.json()['matches']
                if not isinstance(matches, list):
                    raise TypeError
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                raise AgentError('REDDIT_SPELLING_UNAVAILABLE', 503) from exc
            if reviewed['approved'] and not reviewed['issues'] and reviewed['topic'] in {'stories', 'science', 'humor'} and not matches:
                url = 'https://www.reddit.com' + post['permalink']
                full = text + '\n\nИсточник: ' + url + '\nПеревод и редактура с помощью нейросетей.'
                if len(full) > 3500:
                    raise AgentError('REDDIT_POST_TOO_LONG')
                return {'title': translated['title'], 'body': full, 'translation': text,
                        'original': original, 'url': url, 'topic': reviewed['topic']}
            context['previous_translation'] = translated
            context['review_issues'] = reviewed['issues']
            context['spelling_matches'] = matches
        raise AgentError('REDDIT_QUALITY_REJECTED')

    def close(self):
        self.spelling.close()
