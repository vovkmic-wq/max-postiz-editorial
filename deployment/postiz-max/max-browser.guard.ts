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
    }, select: { id: true, parentPostId: true } });
    const identifiers = [...new Set(posts.map(p => p.parentPostId || p.id))];
    const expectedPostId = compare ? req.body?.posts?.[0]?.value?.[0]?.id : undefined;
    if (compare && (req.body?.type !== 'draft' || req.body?.republish
        || !Array.isArray(req.body?.posts) || req.body.posts.length !== 1
        || !Array.isArray(req.body.posts[0]?.value) || req.body.posts[0].value.length !== 1
        || typeof expectedPostId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(expectedPostId)
        || posts.length !== 1 || posts[0].parentPostId || identifiers.length !== 1
        || identifiers[0] !== expectedPostId)) {
      throw new ConflictException('MAX_BROWSER_CAS_INVALID');
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
      if (typeof lease?.owner === 'string') await maxBridgeRequest('/mutation/finish', {
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
