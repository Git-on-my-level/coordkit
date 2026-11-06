import {test} from 'node:test';
import assert from 'node:assert/strict';
import worker, {BusRelay, validRow, retention} from './worker.mjs';

class Storage {
  data = new Map();
  async get(k) { return structuredClone(this.data.get(k)); }
  async put(k, v) { if (typeof k === 'object') for (const [key, val] of Object.entries(k)) this.data.set(key, structuredClone(val)); else this.data.set(k, structuredClone(v)); }
  async delete(k) { this.data.delete(k); }
  async list({start, end, limit}) { return new Map([...this.data].filter(([k]) => k >= start && k < end).sort(([a], [b]) => a.localeCompare(b)).slice(0, limit)); }
  async transaction(fn) { return fn(this); }
}
class Peer {
  a = {site: null, ack: 0}; messages = []; closed = false;
  serializeAttachment(a) { this.a = a; }
  deserializeAttachment() { return this.a; }
  send(m) { this.messages.push(JSON.parse(m)); }
  close() { this.closed = true; }
}
const hello = (site, after = 0) => JSON.stringify({type: 'hello', version: 1, site, after});
const row = (seq, site = 'a') => JSON.stringify({type: 'row', site, seq, row: {id: 'id-' + seq, ts: 'now', frm: 'lead', to: 'worker', kind: 'ask'}});
function setup() {
  const peers = [new Peer(), new Peer()];
  const ctx = {storage: new Storage(), getWebSockets: () => peers, blockConcurrencyWhile: fn => fn()};
  return {peers, ctx, relay: new BusRelay(ctx, {RETAIN_ROWS: '2'})};
}
test('ordered fanout, duplicates, ack, reconnect after hibernation, bounded retention and gap', async () => {
  let {peers: [a, b], peers, ctx, relay} = setup();
  await relay.webSocketMessage(a, hello('a'));
  await relay.webSocketMessage(b, hello('b'));
  await relay.webSocketMessage(a, row(0));
  assert.equal(a.messages.at(-1).duplicate, false);
  await relay.webSocketMessage(a, row(0));
  assert.equal(a.messages.at(-1).duplicate, true);
  assert.equal(b.messages.filter(m => m.type === 'row').length, 1);
  await relay.webSocketMessage(b, JSON.stringify({type: 'ack', relay_seq: 1}));
  assert.equal(b.a.ack, 1);
  await relay.webSocketMessage(a, row(2)); // source gaps are local inbound rows
  relay = new BusRelay(ctx, {RETAIN_ROWS: '2'});
  const reconnected = new Peer(); peers.push(reconnected);
  await relay.webSocketMessage(reconnected, hello('b', 1));
  assert.deepEqual(reconnected.messages.map(m => m.type), ['ready', 'row']);
  assert.equal(reconnected.messages[1].relay_seq, 2);
  await relay.webSocketMessage(a, row(3));
  assert.equal([...ctx.storage.data.keys()].filter(k => k.startsWith('row:')).length, 2);
  const gap = new Peer(); peers.push(gap);
  await relay.webSocketMessage(gap, hello('new'));
  assert.equal(gap.messages[0].type, 'gap'); assert.ok(gap.closed);
  await relay.webSocketMessage(a, row(0));
  assert.equal(await ctx.storage.get('head'), 3);
});
test('reject forged sites, origins, metadata, invalid frames and config', async () => {
  const {peers: [a], relay} = setup();
  await relay.webSocketMessage(a, hello('a'));
  await relay.webSocketMessage(a, row(0, 'b'));
  assert.ok(a.closed);
  assert.equal(validRow({type:'row',site:'a',seq:0,row:{id:'i',ts:'t',frm:'a',to:'b',kind:'ask',origin:'relay:b'}},'a'), false);
  assert.throws(() => retention({RETAIN_ROWS:'0'}));
  assert.throws(() => retention({RETAIN_ROWS:'1001'}));
});
test('edge authentication rejects before touching a Durable Object', async () => {
  let called = false;
  const env = {BUS_NAME: 'test', BUS_SECRET: 'test-secret', BUSES:{idFromName: () => { called=true; }}};
  assert.equal((await worker.fetch(new Request('https://example.test/bus/test'), env)).status, 401);
  assert.equal(called, false);
  const request = new Request('https://example.test/bus/test', {headers:{Authorization:'Bearer test-secret'}});
  assert.equal((await worker.fetch(request, env)).status, 426);
  assert.equal(called, false);
});

test('close callback never transmits reserved abnormal-close codes', () => {
  const {relay} = setup();
  let closed;
  relay.webSocketClose({close: code => {closed = code;}}, 1006);
  assert.equal(closed, 1000);
});
