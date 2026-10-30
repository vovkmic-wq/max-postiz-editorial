"""TEST ONLY: a second, explicitly post-baseline complexity-policy experiment.

The original harness, prompt, approvals and report remain unchanged. This is an
adapted heuristic experiment, not a blind evaluation or proof of translation quality.
"""
import argparse
import copy
import importlib.util
import json
import math
import os
import re
import time
from pathlib import Path

from max_postiz_agent.config import AgentError, fingerprint


def load_baseline():
    path = Path(__file__).with_name('benchmark_cascade.py')
    spec = importlib.util.spec_from_file_location('cascade_baseline_for_complexity', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = load_baseline()
POLICIES = ('review-only', 'length-acronyms-v1')
POLICY_CRITERIA = {'minimum_source_characters': 350, 'english_acronym_regex': r'\b[A-Z]{3,}\b',
                   'combination': 'length_threshold_OR_acronym', 'uses_source_ids_or_gold_references': False}


def complexity(source, policy):
    if policy not in POLICIES:
        raise AgentError('CASCADE_COMPLEXITY_POLICY_INVALID')
    acronyms = sorted(set(re.findall(POLICY_CRITERIA['english_acronym_regex'], source)))
    return {'name': policy, 'source_characters': len(source), 'source_acronyms': acronyms,
            'force_hy': policy == 'length-acronyms-v1' and (len(source) >= 350 or bool(acronyms)),
            'criteria': copy.deepcopy(POLICY_CRITERIA)}


def expected_contract(inputs, prompt, prompt_path, hy_base_url, controls):
    return {'corpus_sha256': inputs['corpus_sha256'], 'argos_sha256': inputs['argos_sha256'],
        'argos_model': inputs['packet']['model'], 'argos_engine': inputs['packet']['engine'],
        'review_model': BASE.REVIEW_MODEL, 'review_prompt_sha256': BASE.digest(prompt.encode()),
        'review_prompt_file_sha256': BASE.digest(prompt_path.read_bytes()),
        'hy_model': BASE.HY_MODEL, 'hy_base_url': hy_base_url,
        'full_body_included': inputs['full_source_sha256'] is not None,
        'full_body_source_sha256': inputs['full_source_sha256'], 'controls_requested': controls,
        'prior_prompt_approvals_comparable': False}


def validate_seed(path, expected, inputs):
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
        if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
                or value['schema_version'] != 1 or value.get('contract') != expected
                or value.get('status') not in {'complete', 'critical_reviewer_failure'}
                or set(value.get('results', {})) != set(inputs['order'])
                or not isinstance(value.get('reviews'), dict) or not isinstance(value.get('controls'), dict)):
            raise ValueError
        for cache_key, reviewed in value['reviews'].items():
            identity = {field: reviewed[field] for field in ('source', 'candidate', 'review_model', 'prompt_sha256')}
            if (not all(isinstance(item, str) for item in identity.values())
                    or fingerprint(identity) != cache_key or reviewed.get('cache_key') != cache_key
                    or identity['review_model'] != expected['review_model']
                    or identity['prompt_sha256'] != expected['review_prompt_sha256']
                    or reviewed.get('status') not in {'approved', 'rejected', 'unverified'}):
                raise ValueError
        for ident, row in value['results'].items():
            argos_input = inputs['primary'][ident]
            if (row.get('id') != ident or row.get('source') != argos_input['source']
                    or row.get('status') not in {'argos_review_accepted', 'hy7b_review_accepted', 'needs_editor'}
                    or row['argos'].get('model') != expected['argos_model']
                    or row['argos'].get('raw_output') != argos_input.get('translated')):
                raise ValueError
            if 'output' in row['argos'] and row['argos']['output'] != BASE.restore_numeric_spelling(
                    row['source'], argos_input['translated'])[0]:
                raise ValueError
            for label in ('argos', 'hy7b'):
                stage = row.get(label)
                if stage is None:
                    continue
                if label == 'hy7b' and stage.get('model') != expected['hy_model']:
                    raise ValueError
                if 'output' in stage:
                    if (not isinstance(stage['output'], str) or not stage['output'].strip()
                            or stage.get('deterministic') != BASE.lexical_checks(row['source'], stage['output'])):
                        raise ValueError
                reviewed = stage.get('review')
                if reviewed and 'cache_key' in reviewed:
                    cache_key = reviewed['cache_key']
                    cached = value['reviews'][cache_key]
                    if (cached['source'] != row['source'] or cached['candidate'] != stage['output']
                            or cached['status'] != reviewed['status']):
                        raise ValueError
                if (label == 'hy7b' and stage.get('status') == 'translated'
                        and (not reviewed or 'cache_key' not in reviewed)):
                    raise ValueError
                duration = stage.get('translation_seconds')
                if duration is not None and (type(duration) not in {int, float}
                                             or not math.isfinite(duration) or duration < 0):
                    raise ValueError
            selected_label = 'argos' if row['status'] == 'argos_review_accepted' else 'hy7b'
            if row['status'] != 'needs_editor':
                stage = row[selected_label]
                if (row.get('selected_output') != stage.get('output')
                        or row.get('selected_model') != stage.get('model')
                        or stage['review']['status'] != 'approved'
                        or not stage['deterministic']['numeric_multiset_match']):
                    raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise AgentError('CASCADE_COMPLEX_SEED_INVALID') from exc
    return value, BASE.digest(raw)


def seed_experiment(report, seed, path, seed_sha256):
    """Copy the completed seed once; never write to its path or mutate its objects."""
    if report.value.get('seed_import_complete'):
        return
    report.value.update(results=copy.deepcopy(seed['results']), reviews=copy.deepcopy(seed['reviews']),
        controls=copy.deepcopy(seed['controls']),
        seed_baseline_outcomes={ident: {'status': row['status'], 'selected_model': row.get('selected_model'),
            'selected_output': row.get('selected_output')} for ident, row in seed['results'].items()},
        seed_review_cache_keys=sorted(seed['reviews']), seed_source_path=str(path), seed_sha256=seed_sha256,
        policy_adapted_after_baseline_observation=True, blind_evaluation=False,
        policy_is_semantic_proof=False, production_routing_changed=False)
    for reviewed in report.value['reviews'].values():
        reviewed.update(seed_reused=True, calls_in_this_experiment=0)
    for row in report.value['results'].values():
        for label in ('argos', 'hy7b'):
            stage = row.get(label)
            if stage and 'review' in stage:
                stage['review'].update(cache_reused=True, seed_reused=True)
            if label == 'hy7b' and stage:
                stage['seed_reused_translation'] = True
    report.value['seed_import_complete'] = True
    report.save()


def force_hy(report, reviewer, fallback, prompt, row):
    """Preserve the original Argos approval; only the policy selection changes."""
    row.setdefault('baseline_selection', {key: row.get(key) for key in (
        'status', 'selected_model', 'selected_output', 'fallback_reason')})
    row.update(status='complexity_policy_running', fallback_reason='COMPLEXITY_POLICY_FORCE_HY')
    row.pop('selected_model', None)
    row.pop('selected_output', None)
    hy = row.get('hy7b')
    if hy is None:
        hy = row['hy7b'] = {'model': BASE.HY_MODEL, 'status': 'translation_in_progress',
            'started_at': BASE.now(), 'created_by_complexity_experiment': True}
        report.save()
        started = time.monotonic()
        try:
            translated, metrics = fallback.translate(row['source'])
            hy.update(status='translated', output=translated, metrics=metrics,
                      deterministic=BASE.lexical_checks(row['source'], translated))
        except Exception as exc:
            hy.update(status='unverified', error=BASE.safe_error(exc))
        finally:
            hy.update(translation_seconds=round(time.monotonic() - started, 6), finished_at=BASE.now())
            report.save()
    elif hy.get('status') == 'translation_in_progress':
        hy.update(status='unverified', error='TRANSLATION_OUTCOME_UNKNOWN_NO_REPLAY', finished_at=BASE.now())
        report.save()
    if hy.get('status') == 'translated':
        if 'review' not in hy:
            cache_key = fingerprint({'source': row['source'], 'candidate': hy['output'],
                'review_model': reviewer.model, 'prompt_sha256': BASE.digest(prompt.encode())})
            if cache_key not in report.value['reviews']:
                keys = row.setdefault('experiment_new_review_keys', [])
                if cache_key not in keys:
                    keys.append(cache_key)
                report.save()
            hy['review'] = BASE.review(report, reviewer, prompt, row['source'], hy['output'])
        if hy['review']['status'] == 'approved' and hy['deterministic']['numeric_multiset_match']:
            row.update(status='hy7b_review_accepted', selected_model=BASE.HY_MODEL, selected_output=hy['output'])
        else:
            row.update(status='needs_editor', reason=hy['review'].get('error') or (
                hy['review']['status'] if hy['deterministic']['numeric_multiset_match']
                else 'NUMERIC_MULTISET_UNVERIFIED'))
    else:
        row.update(status='needs_editor', reason=hy.get('error', 'HY7B_TRANSLATION_UNVERIFIED'))
    report.save()
    return row


def apply_policy(report, reviewer, fallback, prompt, argos_input, policy):
    row = report.value['results'][argos_input['id']]
    if row['source'] != argos_input['source']:
        raise AgentError('CASCADE_RESUME_CANDIDATE_MISMATCH')
    row['complexity_policy'] = complexity(row['source'], policy)
    if row['complexity_policy']['force_hy'] and row['status'] in {
            'argos_review_accepted', 'complexity_policy_running'}:
        return force_hy(report, reviewer, fallback, prompt, row)
    report.save()
    return row


def summarize(report, order):
    critical = BASE.summarize(report, order)
    seed_keys = set(report.value['seed_review_cache_keys'])
    reviews = report.value['reviews']
    new_keys = set(reviews) - seed_keys
    report.value['summary'].update(
        experiment_is_post_baseline_not_blind=True, heuristic_is_quality_proof=False,
        complexity_policy_cases_processed=sum('complexity_policy' in row
            and row['status'] != 'complexity_policy_running' for row in report.value['results'].values()),
        complexity_policy_total_cases=len(order),
        seed_review_count=len(seed_keys), new_review_count=len(new_keys),
        seed_review_seconds=round(sum(reviews[key].get('seconds', 0) for key in seed_keys), 6),
        new_review_seconds=round(sum(reviews[key].get('seconds', 0) for key in new_keys), 6),
        seed_hy7b_translation_seconds=round(sum(row.get('hy7b', {}).get('translation_seconds', 0)
            for row in report.value['results'].values() if row.get('hy7b', {}).get('seed_reused_translation')), 6),
        new_hy7b_translation_seconds=round(sum(row.get('hy7b', {}).get('translation_seconds', 0)
            for row in report.value['results'].values()
            if row.get('hy7b', {}).get('created_by_complexity_experiment')), 6))
    for row in report.value['results'].values():
        hy = row.get('hy7b', {})
        row['additional_measured_seconds_this_experiment'] = round(
            (hy.get('translation_seconds', 0) if hy.get('created_by_complexity_experiment') else 0)
            + sum(reviews[key].get('seconds', 0) for key in set(row.get('experiment_new_review_keys', []))
                  if key in reviews), 6)
    report.save()
    return critical


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/opt/max-postiz'))
    parser.add_argument('--argos-results', type=Path, required=True)
    parser.add_argument('--seed-report', type=Path, required=True)
    parser.add_argument('--output-name', default='cascade-complex-argos-hy7b.json')
    parser.add_argument('--complexity-policy', choices=POLICIES, default='review-only')
    parser.add_argument('--hy-base-url', default='http://127.0.0.1:8094')
    parser.add_argument('--full-article', action='store_true')
    parser.add_argument('--controls', action='store_true')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args(argv)
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}\.json', args.output_name)
            or (args.limit is not None and args.limit < 1)):
        parser.error('Use a private filename.json and a positive --limit')
    root = args.root.resolve()
    argos_path, seed_path = BASE.resolve(root, args.argos_results), BASE.resolve(root, args.seed_report)
    output_path = root / 'data/translation-benchmark' / args.output_name
    for source_path in (argos_path, seed_path):
        if (output_path.resolve() == source_path or (output_path.exists() and source_path.exists()
                                                    and os.path.samefile(output_path, source_path))):
            parser.error('The input/seed report and output report must differ')
    prompt_path = root / 'prompts/CASCADE_REVIEW.txt'
    prompt = prompt_path.read_text(encoding='utf-8')
    inputs = BASE.load_inputs(root / 'deployment/translation/cases.json', argos_path,
        root / 'data/article-20261018-5503.local.json' if args.full_article else None)
    expected = expected_contract(inputs, prompt, prompt_path, args.hy_base_url, args.controls)
    seed, seed_sha256 = validate_seed(seed_path, expected, inputs)
    contract = {**expected, 'complexity_policy': args.complexity_policy,
        'complexity_criteria': POLICY_CRITERIA, 'seed_report_sha256': seed_sha256,
        'secondary_harness_sha256': BASE.digest(Path(__file__).read_bytes()),
        'baseline_harness_sha256': BASE.digest(Path(__file__).with_name('benchmark_cascade.py').read_bytes())}
    report = BASE.Report(output_path, contract, inputs, args.resume)
    seed_experiment(report, seed, seed_path, seed_sha256)
    reviewer = fallback = None
    exit_code = 1
    try:
        BASE.load_env(root)
        settings = BASE.Settings.load(root)
        reviewer = BASE.OpenCode(settings)
        reviewer.model = BASE.REVIEW_MODEL
        fallback = BASE.DedicatedTranslator({'model': BASE.HY_MODEL, 'base_url': args.hy_base_url})
        selected = inputs['order'][:args.limit] if args.limit else inputs['order']
        for ident in selected:
            apply_policy(report, reviewer, fallback, prompt, inputs['primary'][ident], args.complexity_policy)
            summarize(report, inputs['order'])
        critical = summarize(report, inputs['order'])
        processed = {ident for ident, row in report.value['results'].items() if 'complexity_policy' in row
                     and row['status'] != 'complexity_policy_running'}
        report.value.update(last_run_finished_at=BASE.now(),
            status='critical_reviewer_failure' if critical else
                   'complete' if processed == set(inputs['order']) else 'partial')
        exit_code = 2 if critical else 0
    except Exception as exc:
        report.value.update(status='interrupted_or_failed', error=BASE.safe_error(exc), last_run_finished_at=BASE.now())
        if summarize(report, inputs['order']):
            exit_code = 2
    finally:
        if reviewer is not None:
            reviewer.client.close()
        if fallback is not None:
            fallback.client.close()
        report.value['exit_code'] = exit_code
        report.save()
    print(json.dumps({'status': report.value['status'], 'exit_code': exit_code,
                      'summary': report.value.get('summary'), 'report': str(output_path)}, ensure_ascii=False), flush=True)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
