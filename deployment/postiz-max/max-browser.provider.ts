import { SocialAbstract } from '@gitroom/nestjs-libraries/integrations/social.abstract';
import { SocialProvider, AuthTokenDetails, PostDetails, PostResponse, PendingCheckResponse } from '@gitroom/nestjs-libraries/integrations/social/social.integrations.interface';
import { makeSecureId } from '@gitroom/nestjs-libraries/services/make.secure.id';
import { Integration } from '@prisma/client';
import { ApplicationFailure } from '@temporalio/activity';

export async function maxBridgeRequest(path: string, data: any): Promise<any> {
  const base = process.env.MAX_BRIDGE_URL || 'http://host.docker.internal:8090/api/max-bridge';
  const key = process.env.MAX_POSTIZ_BRIDGE_KEY;
  const parsed = new URL(base);
  if (!key || !['localhost', '127.0.0.1', 'host.docker.internal'].includes(parsed.hostname) || parsed.protocol !== 'http:') {
    throw new Error('MAX_BROWSER_BRIDGE_NOT_CONFIGURED');
  }
  let response: Response;
  try {
    response = await fetch(base + path, { method: 'POST', headers: {
      'Content-Type': 'application/json', 'X-Max-Bridge-Key': key,
    }, body: JSON.stringify(data), signal: AbortSignal.timeout(60000) });
  } catch {
    throw new Error('MAX_BROWSER_AGENT_UNAVAILABLE');
  }
  const result = await response.json();
  if (!response.ok) throw new Error(result.code || 'MAX_BROWSER_BRIDGE_REJECTED');
  return result;
}

export class MaxBrowserProvider extends SocialAbstract implements SocialProvider {
  identifier = 'max-browser';
  name = 'MAX — браузер';
  toolTip = 'Согласование в локальной редакции. Публикация через браузер MAX без бота.';
  editor = 'normal' as const;
  isBetweenSteps = false;
  scopes: string[] = [];
  override maxConcurrentJob = 1;
  maxLength() { return 3500; }
  override async checkValidity(posts: Array<{ path: string }[]>): Promise<string | true> {
    return posts.length !== 1 || posts[0].length > 1 ? 'MAX browser supports one post and one attachment.' : true;
  }
  async generateAuthUrl() {
    const state = makeSecureId(32), codeVerifier = makeSecureId(32);
    const result = await maxBridgeRequest('/connect', {
      target_id: process.env.MAX_BRIDGE_TARGET_ID || 'max_test', state, verifier: codeVerifier,
    });
    const url = new URL('/integrations/social/max-browser', process.env.FRONTEND_URL!);
    url.searchParams.set('code', result.code);
    url.searchParams.set('state', state);
    return { url: url.toString(), state, codeVerifier };
  }
  async authenticate(params: { code: string; codeVerifier: string }): Promise<AuthTokenDetails> {
    return maxBridgeRequest('/exchange', { code: params.code, verifier: params.codeVerifier });
  }
  async refreshToken(): Promise<AuthTokenDetails> {
    throw new Error('Reconnect MAX browser channel after token expiration.');
  }
  async postPending(id: string, token: string, posts: PostDetails[], integration: Integration): Promise<PostResponse[]> {
    if (posts.length !== 1) throw ApplicationFailure.create({ message: 'MAX threads are unsupported', nonRetryable: true });
    const postId = posts[0].id;
    await maxBridgeRequest('/submit', { channel_token: token, integration_id: integration.id, post_id: postId });
    return [{ id: postId, postId, releaseURL: '', status: 'pending', pendingData: { postId } }];
  }
  async post(id: string, token: string, posts: PostDetails[], integration: Integration): Promise<PostResponse[]> {
    return this.postPending(id, token, posts, integration);
  }
  override async checkPostStatus(token: string, pending: any, integration: Integration): Promise<PendingCheckResponse> {
    const result = await maxBridgeRequest('/status', { channel_token: token,
      integration_id: integration.id, post_id: pending.postId });
    if (result.state === 'published') return { status: 'completed', postId: result.external_id,
      releaseURL: result.permalink || '' };
    if (['failed', 'cancelled'].includes(result.state)) {
      throw ApplicationFailure.create({ message: 'MAX delivery stopped: ' + (result.error || result.state), nonRetryable: true });
    }
    return { status: 'pending', pendingData: pending };
  }
}
