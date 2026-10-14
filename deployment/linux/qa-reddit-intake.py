"""Synthetic input in a disposable PG schema; never collect Reddit or publish."""
import argparse
import json
import os
import re
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from max_postiz_agent.cli import load_env
from max_postiz_agent.config import AgentError, Settings, fingerprint
from max_postiz_agent.db import Database, uid
from max_postiz_agent.hub import HubService
from max_postiz_agent.hub_models import RedditSubmission


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path('/opt/max-postiz'))
    parser.add_argument('--live-models', action='store_true')
    args = parser.parse_args()
    load_env(args.root)
    settings = Settings.load(args.root)
    admin = create_engine(os.environ['DATABASE_URL'])
    schema = 'reddit_probe_' + uid().replace('-', '')
    assert re.fullmatch(r'reddit_probe_[a-f0-9]{32}', schema)
    with admin.begin() as connection:
        connection.execute(text('CREATE SCHEMA "' + schema + '"'))
    service, db = None, None
    try:
        url = make_url(os.environ['DATABASE_URL']).update_query_dict({'options': '-csearch_path=' + schema})
        db = Database(settings, url=url.render_as_string(hide_password=False))
        # Pre-deployment probes must use the tested source migrations, not the older runtime tree.
        db.root = Path(__file__).resolve().parents[2]
        db.initialize()
        service = HubService(settings, db)
        service.activate()
        # Prevent an optional production setting from exporting this synthetic probe to Postiz.
        service.reddit_intake.options = lambda: {
            'processing_enabled': True, 'stage_postiz': False, 'mail_target_id': ''}
        targets = [t for t in service.publishing['targets'] if t['enabled'] and t['platform'] == 'max']
        assert targets
        source = dict(title='The quiet garden',
            original=('A gardener planted flowers near the gate. Every morning a neighbor stopped '
                      'to admire them. When the gardener went away, the neighbor watered the plants. '
                      'The gardener returned and found the flowers blooming. They sat in the garden '
                      'and talked about how a small kindness had brought them together.'),
            url='https://www.reddit.com/r/stories/comments/codextest/synthetic_probe/',
            target_id=targets[0]['id'])
        receipt = service.reddit_intake.submit(**source)
        assert service.reddit_intake.submit(**source, origin='ifttt')['duplicate']
        with db.session() as session:
            row = session.get(RedditSubmission, receipt['id'])
            digest = fingerprint({k: getattr(row, k) for k in ('title', 'original', 'url')})
        service.reddit_intake.attest_rights(receipt['id'],
            'Synthetic original text authored only for this isolated technical probe', digest)
        if args.live_models:
            service.reddit_intake.process_one()
        with db.session() as session:
            row = session.get(RedditSubmission, receipt['id'])
            assert row.state == ('ready' if args.live_models else 'queued'), row.error
            assert session.scalar(text('SELECT count(*) FROM hub_dispatches')) == 0
            report = {'state': row.state, 'duplicate_verified': True,
                'live_models': args.live_models, 'reddit_requests': 0,
                'external_posts': 0, 'postiz_drafts': 0, 'production_rows_changed': 0}
        output = args.root / 'data/reddit-intake-quality-check.json'
        output.write_text(json.dumps(report), encoding='utf-8')
        print(json.dumps(report))
    except AgentError as exc:
        print(json.dumps({'state': 'failed', 'code': exc.code, 'external_posts': 0}))
        raise SystemExit(1) from None
    except Exception:
        print(json.dumps({'state': 'failed', 'code': 'REDDIT_PROBE_FAILED', 'external_posts': 0}))
        raise SystemExit(1) from None
    finally:
        if service:
            service.close()
        elif db:
            db.engine.dispose()
        with admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        admin.dispose()


if __name__ == '__main__':
    main()
