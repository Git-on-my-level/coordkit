// One self-hosted bus per deployment. Auth resolves a bus/site principal at the
// edge; the ordered-log protocol is independent of future credential issuers.
export const MAX_MESSAGE = 65536;
export const SITE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
const integer = n => Number.isSafeInteger(n) && n >= 0;
const key = seq => 'row:' + String(seq).padStart(16, '0');

export function validRow(frame, site) {
  const row = frame?.row;
  return frame?.type === 'row' && frame.site === site && integer(frame.seq) &&
    row && !Array.isArray(row) && typeof row === 'object' &&
    ['id', 'ts', 'frm', 'to', 'kind'].every(k => typeof row[k] === 'string') && row.id &&
    !String(row.origin || '').startsWith('relay:') && !('_relay' in row);
}

export function retention(env) {
  const count = Number(env.RETAIN_ROWS || 256);
  if (!Number.isInteger(count) || count < 1 || count > 1000) throw Error('invalid_retention');
  return count;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== '/bus/' + env.BUS_NAME || url.search) return new Response('Not found', {status: 404});
    // Never put credentials in query parameters, attachments or diagnostic text.
    const provided = request.headers.get('Authorization') || '';
    const expected = 'Bearer ' + env.BUS_SECRET;
    const digest = async text => new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text)));
    if (!env.BUS_SECRET || provided.length > 4103) return new Response('Unauthorized', {status: 401});
    const [a, b] = await Promise.all([digest(provided), digest(expected)]);
    let different = 0;
    for (let i = 0; i < a.length; i++) different |= a[i] ^ b[i];
    if (different) return new Response('Unauthorized', {status: 401});
    if (request.headers.get('Upgrade')?.toLowerCase() !== 'websocket') return new Response('Upgrade required', {status: 426});
    return env.BUSES.get(env.BUSES.idFromName(env.BUS_NAME)).fetch(request);
  }
};

export class BusRelay {
  constructor(ctx, env) { this.ctx = ctx; this.env = env; }

  async fetch() {
    const pair = new WebSocketPair();
    this.ctx.acceptWebSocket(pair[1]);
    pair[1].serializeAttachment({site: null, ack: 0});
    return new Response(null, {status: 101, webSocket: pair[0]});
  }

  async webSocketMessage(ws, message) {
    // Serialize replay + publish across async storage calls, including hibernation.
    await this.ctx.blockConcurrencyWhile(async () => {
      try { await this.message(ws, message); }
      catch { ws.close(1008, 'relay_protocol_error'); }
    });
  }

  async message(ws, message) {
    if (typeof message !== 'string' || new TextEncoder().encode(message).length > MAX_MESSAGE) throw Error('size');
    const f = JSON.parse(message);
    const a = ws.deserializeAttachment();
    const storage = this.ctx.storage;
    if (!a.site) {
      if (f?.type !== 'hello' || f.version !== 1 || typeof f.site !== 'string' || !SITE.test(f.site) || !integer(f.after)) throw Error('hello');
      const head = (await storage.get('head')) || 0;
      const oldest = (await storage.get('oldest')) || 1;
      if (f.after > head || f.after < oldest - 1) {
        ws.send(JSON.stringify({type: 'gap', oldest, head}));
        ws.close(1008, 'relay_retention_gap');
        return;
      }
      const sites = (await storage.get('sites')) || {};
      if (!Object.hasOwn(sites, f.site)) {
        if (Object.keys(sites).length >= 64) throw Error('site_limit');
        sites[f.site] = {seq: -1};
        await storage.put('sites', sites);
      }
      // A site is one durable writer identity, never shared by two live runners.
      for (const other of this.ctx.getWebSockets()) {
        if (other !== ws && other.deserializeAttachment()?.site === f.site) other.close(1008, 'relay_site_replaced');
      }
      ws.serializeAttachment({site: f.site, ack: f.after});
      ws.send(JSON.stringify({type: 'ready', head}));
      const rows = await storage.list({start: key(f.after + 1), end: 'row;', limit: 1000});
      for (const row of rows.values()) ws.send(JSON.stringify(row));
      return;
    }
    if (f?.type === 'ping') { ws.send(JSON.stringify({type: 'pong'})); return; }
    if (f?.type === 'ack' && integer(f.relay_seq)) {
      const head = (await storage.get('head')) || 0;
      if (f.relay_seq < a.ack || f.relay_seq > head) throw Error('ack');
      ws.serializeAttachment({...a, ack: f.relay_seq});
      return;
    }
    if (!validRow(f, a.site) || new TextEncoder().encode(JSON.stringify(f)).length > MAX_MESSAGE - 64) throw Error('row');
    const result = await storage.transaction(async tx => {
      const sites = await tx.get('sites');
      const prior = sites[a.site];
      // High-water marks survive retention; ancient replays cannot resurrect rows.
      if (f.seq <= prior.seq) return {duplicate: true};
      const head = ((await tx.get('head')) || 0) + 1;
      const oldest = (await tx.get('oldest')) || 1;
      const nextOldest = Math.max(oldest, head - retention(this.env) + 1);
      const row = {type: 'row', relay_seq: head, site: a.site, seq: f.seq, row: f.row};
      sites[a.site] = {seq: f.seq};
      await tx.put({head, oldest: nextOldest, sites, [key(head)]: row});
      for (let i = oldest; i < nextOldest; i++) await tx.delete(key(i));
      return {row};
    });
    if (result.row) {
      for (const peer of this.ctx.getWebSockets()) {
        if (peer.deserializeAttachment()?.site) {
          try { peer.send(JSON.stringify(result.row)); }
          catch { peer.close(1011, 'relay_reconnect'); }
        }
      }
    }
    ws.send(JSON.stringify({type: 'accepted', site: a.site, seq: f.seq, duplicate: Boolean(result.duplicate)}));
  }

  webSocketClose(ws) { ws.close(1000, 'closed'); }
  webSocketError(ws) { ws.close(1011, 'relay_reconnect'); }
}
