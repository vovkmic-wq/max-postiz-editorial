"""Positive reviewer control, separate from any generated/rejected candidate."""
import json
from pathlib import Path

import smoke_quality as smoke


def main():
    root = Path('/opt/max-postiz')
    smoke.load_env(root)
    settings = smoke.Settings.load(root)
    translator = smoke.configured_translator(settings)
    reviewer = smoke.OpenCode(settings)
    reviewer.model = smoke.REVIEW_MODEL
    report = smoke.PrivateReport(root, False, 'quality-reference-control.json')
    report.value.update(translation_model=None, review_model=reviewer.model,
        candidate_origin='Author-supplied positive control, not a model translation or editorial approval',
        controlled_source_is_synthetic=True, publishing_requested=False)
    cases = json.loads((root / 'deployment/translation/cases.json').read_text())['cases']
    source = next(case['text'] for case in cases if case['id'] == 'controlled-negation-modality')
    candidate = {'title': 'Наблюдения космического аппарата',
        'body': 'Космический аппарат не обнаружил жизни. Наблюдения могут поддерживать гипотезу, '
                'но они не доказывают, что Марс был обитаемым.'}
    try:
        verdict = smoke.check(report, settings, translator, reviewer, 'literal-reference-positive',
            candidate, smoke.CONTROLLED_TITLE, source, smoke.CONTROLLED_URL)
        report.value.update(status=verdict, finished_at=smoke.timestamp())
        report.save()
        print(json.dumps({'verdict': verdict, 'report': str(report.path)}), flush=True)
        return 0 if verdict == 'passed' else 1
    finally:
        translator.client.close()
        reviewer.client.close()


if __name__ == '__main__':
    raise SystemExit(main())
