import { CanActivate, ConflictException, ExecutionContext, Injectable } from '@nestjs/common';
import { PrismaService } from '@gitroom/nestjs-libraries/database/prisma/prisma.service';
import { maxBridgeRequest } from '@gitroom/nestjs-libraries/integrations/social/max-browser.provider';

@Injectable()
export class MaxBrowserMutationGuard implements CanActivate {
  constructor(private prisma: PrismaService) {}
  async canActivate(context: ExecutionContext): Promise<boolean> {
    const req = context.switchToHttp().getRequest(), res = context.switchToHttp().getResponse();
    const expectedHash = req.headers?.['x-max-postiz-expected-hash'];
    const compare = expectedHash !== undefined;
    if (compare && (!process.env.MAX_POSTIZ_BRIDGE_KEY || typeof expectedHash !== 'string'
        || !/^[a-f0-9]{64}$/.test(expectedHash) || req.method !== 'POST'
        || !/^\/(public\/v1\/)?posts\/?$/.test(req.path))) {
      throw new ConflictException('MAX_BROWSER_CAS_INVALID');
    }
    if (!process.env.MAX_POSTIZ_BRIDGE_KEY || !['POST', 'PUT', 'PATCH', 'DELETE'].includes(req.method)
        || !/^\/(public\/v1\/)?posts(\/|$)/.test(req.path)) return true;
    const candidates = new Set<string>(req.path.split('/').filter((s: string) => s.length < 100));
    const walk = (value: any) => {
      if (Array.isArray(value)) value.forEach(walk);
      else if (value && typeof value === 'object') for (const [key, item] of Object.entries(value)) {
        if (['id', 'group'].includes(key) && typeof item === 'string' && item.length < 100) candidates.add(item);
        else if (typeof item === 'object') walk(item);
      }
    };
    walk(req.body);
    const ids = [...candidates];
    if (ids.length > 100) throw new ConflictException('MAX_BROWSER_TOO_MANY_MUTATIONS');
    const posts = await this.prisma.post.findMany({ where: {
      OR: [{ id: { in: ids } }, { group: { in: ids } }, { parentPostId: { in: ids } }],
      integration: { providerIdentifier: 'max-browser' },
    }, select: { id: true, parentPostId: true, group: true, integrationId: true,
      state: true, content: true, image: true, settings: true } });
    const identifiers = [...new Set(posts.map(p => p.parentPostId || p.id))];
    const expectedPostId = compare ? req.body?.posts?.[0]?.value?.[0]?.id : undefined;
    if (compare && (!['draft', 'schedule'].includes(req.body?.type) || req.body?.republish
        || !Array.isArray(req.body?.posts) || req.body.posts.length !== 1
        || !Array.isArray(req.body.posts[0]?.value) || req.body.posts[0].value.length !== 1
        || typeof expectedPostId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(expectedPostId)
        || posts.length !== 1 || posts[0].parentPostId || identifiers.length !== 1
        || identifiers[0] !== expectedPostId || posts[0].state !== 'DRAFT'
        || typeof req.body.posts[0].group !== 'string' || !req.body.posts[0].group
        || req.body.posts[0].group !== posts[0].group
        || req.body.posts[0]?.integration?.id !== posts[0].integrationId)) {
      throw new ConflictException('MAX_BROWSER_CAS_INVALID');
    }
    if (compare && req.body.type === 'schedule') {
      const post = req.body.posts[0], value = post.value[0], media = value.image;
      const date = req.body.date;
      // The agent sends UTC, including Python's six fractional digits. A real
      // future calendar instant is checked, never substituted or rounded here.
      const parsed = typeof date === 'string' ? Date.parse(date) : NaN;
      if (req.body.republish !== false || req.body.shortLink !== false
          || !Array.isArray(req.body.tags) || req.body.tags.length !== 0
          || req.body.inter !== undefined || post.date !== undefined || value.delay !== undefined
          || typeof date !== 'string'
          || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(date)
          || !Number.isFinite(parsed) || parsed <= Date.now()
          || new Date(parsed).toISOString().slice(0, 19) !== date.slice(0, 19)
          || !Array.isArray(media) || media.length !== 1
          || !media[0] || typeof media[0] !== 'object' || Array.isArray(media[0])
          || Object.keys(media[0]).some(key => !['id', 'path', 'alt', 'thumbnail'].includes(key))
          || typeof media[0].id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(media[0].id)
          || typeof media[0].path !== 'string' || !media[0].path.trim() || media[0].path.length > 4096
          || /[\x00-\x1f\x7f]/.test(media[0].path)
          || (media[0].alt != null && typeof media[0].alt !== 'string')
          || (media[0].thumbnail != null && typeof media[0].thumbnail !== 'string')
          || typeof value.content !== 'string' || value.content !== posts[0].content) {
        throw new ConflictException('MAX_BROWSER_CAS_SCHEDULE_INVALID');
      }
      const normalized = (item: any): any => Array.isArray(item) ? item.map(normalized)
        : item && typeof item === 'object' ? Object.fromEntries(Object.keys(item).sort()
          .map(key => [key, normalized(item[key])])) : item;
      const stored = (item: any) => typeof item === 'string' ? JSON.parse(item) : item;
      try {
        if (JSON.stringify(normalized(media)) !== JSON.stringify(normalized(stored(posts[0].image)))
            || JSON.stringify(normalized(post.settings)) !== JSON.stringify(normalized(stored(posts[0].settings)))) {
          throw new Error('changed schedule snapshot');
        }
      } catch (_) {
        throw new ConflictException('MAX_BROWSER_CAS_SCHEDULE_CHANGED');
      }
    }
    if (!identifiers.length) return true;
    let lease: any;
    try { lease = await maxBridgeRequest('/mutation/start', { identifiers,
      ...(compare ? { expected_hash: expectedHash, expected_post_id: expectedPostId } : {}),
    }); }
    catch (e: any) { throw new ConflictException(e.message || 'MAX_BROWSER_MUTATION_BLOCKED'); }
    if (compare && (typeof lease?.owner !== 'string' || !lease.owner || lease.owner.length > 36
        || lease.cas_verified !== true || lease.expected_hash !== expectedHash
        || lease.expected_post_id !== expectedPostId)) {
      // A known pre-controller failure is safe to finish, never to overwrite.
      if (typeof lease?.owner === 'string' && lease.owner && lease.owner.length <= 36) await maxBridgeRequest('/mutation/finish', {
        identifiers, owner: lease.owner,
      }).catch(() => {});
      throw new ConflictException('MAX_BROWSER_CAS_UNCONFIRMED');
    }
    // A lost completion deliberately keeps the lease: the owner must resolve it.
    res.once('finish', () => { void maxBridgeRequest('/mutation/finish', {
      identifiers, owner: lease.owner,
    }).catch(() => {}); });
    if (compare) res.setHeader('X-Max-Postiz-CAS-Verified', expectedHash);
    return true;
  }
}
