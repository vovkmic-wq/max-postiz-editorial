"""Live translation/review smoke checks; never create DB entities or publish content.

Run from the application environment on VM112. Private source text, raw model
outputs and review traces are stored only under data/translation-benchmark.
Exit 0: all requested checks passed; 1: rejection/infrastructure/inconclusive;
2: the independent check approved the deliberately false negative candidate.
"""
import argparse
import copy
import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from max_postiz_agent.adapters import OpenCode
from max_postiz_agent.article_quality import ArticleQuality
from max_postiz_agent.cli import load_env
from max_postiz_agent.config import AgentError, Settings
from max_postiz_agent.translation import MODELS, configured_translator

REVIEW_MODEL = 'proxmox/qwen25-coder-7b'
REVIEW_MODELS = (REVIEW_MODEL, 'proxmox/qwen35-9b')
CONTROLLED_TITLE = 'The spacecraft observations'
CONTROLLED_URL = 'https://example.org/translation-quality-smoke/'
FALSE_TRANSLATION = ('Космический аппарат обнаружил жизнь. Эти наблюдения доказывают, '
                     'что Марс был обитаемым.')


def timestamp():
    return datetime.now(ZoneInfo('Europe/Moscow')).isoformat()


class PrivateReport:
    def __init__(self, root, full_article, output_name='quality-smoke.json'):
        self.path = root / 'data/translation-benchmark' / output_name
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.value = {'schema_version': 1, 'started_at': timestamp(),
                      'full_article_requested': full_article,
                      'publishing_requested': False, 'checks': [], 'translations': []}
        self.save()

    def save(self):
        temporary = self.path.with_suffix('.json.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(self.value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)


def error_code(exc):
    # Exception messages from HTTP clients may include credentials or source text.
    return exc.code if isinstance(exc, AgentError) else type(exc).__name__


def translate(report, translator, case_id, title, source):
    row = {'case': case_id, 'source_untrusted': {'title': title, 'body': source},
           'translation_model': translator.model, 'generated_in_this_run': True,
           'status': 'translating', 'started_at': timestamp()}
    report.value['translations'].append(row)
    report.save()
    started = time.monotonic()
    try:
        raw, metrics = translator.generate_text(json.dumps({
            'source_untrusted': {'title': title, 'body': source}}, ensure_ascii=False))
        row.update(raw_output=raw, metrics=metrics)
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {'title', 'body'}
                or any(not isinstance(item, str) or not item.strip() for item in value.values())):
            raise AgentError('TRANSLATION_RESPONSE_INVALID')
        row.update(status='translated', candidate=value)
        return value
    except Exception as exc:
        row.update(status='failed', error=error_code(exc))
        return None
    finally:
        row['seconds'] = round(time.monotonic() - started, 3)
        row['finished_at'] = timestamp()
        report.save()


def reuse_translation(report, path, case_id, title, source):
    """Reuse one validated exact source/candidate pair without calling generation."""
    raw = path.read_bytes()
    try:
        previous = json.loads(raw)
        if (not isinstance(previous, dict) or type(previous.get('schema_version')) is not int
                or previous['schema_version'] != 1 or not isinstance(previous.get('translations'), list)
                or previous.get('translation_model') not in MODELS):
            raise ValueError
        expected_source = {'title': title, 'body': source}
        matching = [row for row in previous['translations'] if isinstance(row, dict)
                    and row.get('case') == case_id and row.get('source_untrusted') == expected_source]
        if len(matching) != 1:
            raise ValueError
        row = matching[0]
        candidate, metrics = row['candidate'], row['metrics']
        source_model = row.get('translation_model', previous['translation_model'])
        if (row.get('status') != 'translated' or not isinstance(candidate, dict)
                or set(candidate) != {'title', 'body'}
                or any(not isinstance(value, str) or not value.strip() for value in candidate.values())
                or source_model not in MODELS
                or not isinstance(metrics, dict) or metrics.get('model') != source_model
                or not isinstance(row.get('raw_output'), str)
                or json.loads(row['raw_output']) != candidate):
            raise ValueError
    except (ValueError, KeyError, TypeError) as exc:
        raise AgentError('TRANSLATION_REUSE_INVALID') from exc
    reused = copy.deepcopy(row)
    reused.update(translation_model=source_model, generated_in_this_run=False,
        reused_from_report=str(path), source_report_sha256=hashlib.sha256(raw).hexdigest(),
        original_translation_seconds=row.get('seconds'), seconds=0.0,
        started_at=timestamp(), finished_at=timestamp())
    reused['metrics'] = {**metrics, 'candidate_reused_from_report': True}
    reused['reuse_trace'] = [{'phase': 'translation_reused_without_generation',
        'source_report': str(path), 'source_report_sha256': reused['source_report_sha256'],
        'exact_source_and_raw_candidate_validated': True, 'at': timestamp()}]
    report.value['translations'].append(reused)
    report.value.update(reused_source_candidate=True, reused_translation_model=source_model,
                        reuse_translation_report=str(path))
    report.save()
    return copy.deepcopy(candidate)


def classify_check(row, negative):
    """Distinguish a real semantic refusal from an unavailable or invalid reviewer."""
    reviews = [event['review'] for event in row['trace'] if event['phase'] == 'review_parsed']
    review = reviews[-1] if reviews else None
    if not negative:
        return 'passed' if row['status'] == 'accepted' else 'failed'
    if row['status'] == 'accepted' or (review and review.get('approved') is True
                                     and not review.get('issues')):
        # Spelling rejection cannot hide a reviewer approving an invented claim.
        return 'critical_false_claim_approved'
    if (row.get('error') == 'ARTICLE_QUALITY_REJECTED' and review
            and (review.get('approved') is False or review.get('issues'))):
        return 'passed_negative_blocked'
    return 'inconclusive_negative_check'


def check(report, settings, translator, reviewer, case_id, candidate, original_title,
          original, url, negative=False):
    row = {'case': case_id, 'negative_control': negative, 'status': 'checking',
           'review_model': reviewer.model,
           'candidate': candidate, 'source_untrusted': {
               'title': original_title, 'body': original, 'url': url},
           'trace': [], 'started_at': timestamp()}
    report.value['checks'].append(row)
    report.save()

    def trace(event):
        row['trace'].append({**event, 'at': timestamp()})
        report.save()

    quality = None
    started = time.monotonic()
    try:
        quality = ArticleQuality(translator, reviewer, prompt_dir=settings.root / 'prompts', trace=trace)
        # Exactly one attempt. Do not resample or soften a refusal.
        result = quality.check_candidate(candidate['title'], candidate['body'],
                                         original_title, original, url)
        row.update(status='accepted', result=result)
    except Exception as exc:
        row.update(status='rejected_or_failed', error=error_code(exc))
    finally:
        if quality is not None:
            quality.close()
        row['seconds'] = round(time.monotonic() - started, 3)
        row['finished_at'] = timestamp()
        row['verdict'] = classify_check(row, negative)
        report.save()
    return row['verdict']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/opt/max-postiz'))
    parser.add_argument('--full-article', action='store_true',
                        help='Also translate and review the complete stored NASA 5503 article.')
    parser.add_argument('--review-model', choices=REVIEW_MODELS, default=REVIEW_MODEL)
    parser.add_argument('--output-name', default='quality-smoke.json',
                        help='A JSON filename under data/translation-benchmark (no directory).')
    parser.add_argument('--reuse-translation-report', type=Path,
                        help='Reuse a validated controlled candidate; relative paths are relative to --root.')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}\.json', args.output_name):
        parser.error('--output-name must be a plain filename.json without directory components')
    output_path = (root / 'data/translation-benchmark' / args.output_name).resolve()
    reuse_path = args.reuse_translation_report
    if reuse_path is not None:
        reuse_path = (reuse_path if reuse_path.is_absolute() else root / reuse_path).resolve()
        if (reuse_path == output_path or (reuse_path.exists() and output_path.exists()
                                         and os.path.samefile(reuse_path, output_path))):
            parser.error('The source translation report and output report must differ')
    # All path checks precede report creation so a reuse source cannot be truncated.
    report = PrivateReport(root, args.full_article, args.output_name)
    translator = reviewer = None
    exit_code = 1
    try:
        load_env(root)
        settings = Settings.load(root)
        translator = configured_translator(settings)
        if translator is None:
            raise AgentError('DEDICATED_TRANSLATION_REQUIRED')
        reviewer = OpenCode(settings)
        reviewer.model = args.review_model
        report.value.update(translation_model=translator.model, review_model=reviewer.model,
                            reused_source_candidate=False)
        cases = json.loads((root / 'deployment/translation/cases.json').read_text(encoding='utf-8'))['cases']
        controlled = next(item for item in cases if item['id'] == 'controlled-negation-modality')
        source = controlled['text']
        report.value['controlled_source_is_synthetic'] = True
        report.value['controlled_url_is_format_fixture_only'] = True
        candidate = (reuse_translation(report, reuse_path, controlled['id'], CONTROLLED_TITLE, source)
            if reuse_path is not None else
            translate(report, translator, controlled['id'], CONTROLLED_TITLE, source))
        verdicts = []
        if candidate is not None:
            verdicts.append(check(report, settings, translator, reviewer, controlled['id'] + '-positive',
                candidate, CONTROLLED_TITLE, source, CONTROLLED_URL))
        else:
            verdicts.append('positive_translation_failed')
        negative = {'title': candidate['title'] if candidate else 'Наблюдения космического аппарата',
                    'body': FALSE_TRANSLATION}
        verdicts.append(check(report, settings, translator, reviewer, controlled['id'] + '-negative',
            negative, CONTROLLED_TITLE, source, CONTROLLED_URL, negative=True))
        if 'critical_false_claim_approved' in verdicts:
            # Preserve this priority even if the optional full-article phase later fails.
            exit_code = 2
        if args.full_article:
            article = json.loads((root / 'data/article-20261018-5503.local.json').read_text(encoding='utf-8'))
            candidate = translate(report, translator, 'nasa-5503-full', article['title'], article['original'])
            verdicts.append(check(report, settings, translator, reviewer, 'nasa-5503-full-positive',
                candidate, article['title'], article['original'], article['url'])
                if candidate is not None else 'full_article_translation_failed')
        exit_code = (2 if 'critical_false_claim_approved' in verdicts else
                     0 if all(value in {'passed', 'passed_negative_blocked'} for value in verdicts) else 1)
    except Exception as exc:
        report.value['setup_or_execution_error'] = error_code(exc)
    finally:
        if translator is not None:
            translator.client.close()
        if reviewer is not None:
            reviewer.client.close()
        report.value.update(finished_at=timestamp(), exit_code=exit_code,
            status='passed' if exit_code == 0 else 'critical_failure' if exit_code == 2 else 'not_passed')
        report.save()
    # Keep private source/model output out of stdout and service journals.
    print(json.dumps({'status': report.value['status'], 'exit_code': exit_code,
                      'report': str(report.path)}, ensure_ascii=False), flush=True)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
