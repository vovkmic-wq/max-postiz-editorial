import { CanActivate, ConflictException, ExecutionContext, Injectable } from '@nestjs/common';
import { PrismaService } from '@gitroom/nestjs-libraries/database/prisma/prisma.service';
import { maxBridgeRequest } from '@gitroom/nestjs-libraries/integrations/social/max-browser.provider';

@Injectable()
export class MaxBrowserMutationGuard implements CanActivate {
  constructor(private prisma: PrismaService) {}
  async canActivate(context: ExecutionContext): Promise<boolean> {
    const req = context.switchToHttp().getRequest(), res = context.switchToHttp().getResponse();
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
    if (!identifiers.length) return true;
    let lease: any;
    try { lease = await maxBridgeRequest('/mutation/start', { identifiers }); }
    catch (e: any) { throw new ConflictException(e.message || 'MAX_BROWSER_MUTATION_BLOCKED'); }
    // A lost completion deliberately keeps the lease: the owner must resolve it.
    res.once('finish', () => { void maxBridgeRequest('/mutation/finish', {
      identifiers, owner: lease.owner,
    }).catch(() => {}); });
    return true;
  }
}
