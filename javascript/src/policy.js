import { performance } from 'node:perf_hooks';
import { isObject, Rejection } from './json.js';

export class Limiter {
  constructor(clock = () => performance.now() / 1000) {
    this.clock = clock;
    this.windows = new Map();
  }

  consume(client, scope, rate) {
    const now = this.clock();
    const key = JSON.stringify([client, scope]);
    let window = this.windows.get(key) ?? [];
    window = window.filter(time => time > now - rate.window_seconds);
    this.windows.set(key, window);
    if (window.length >= rate.requests) {
      throw new Rejection(429, -32029, 'Rate limit exceeded',
        Math.max(1, Math.ceil(window[0] + rate.window_seconds - now)));
    }
    // No await between checking and consuming: atomic in this Node process.
    window.push(now);
  }
}

export function checkTool(client, params, settings, validators) {
  if (Object.keys(params).some(key => !['name', 'arguments'].includes(key))) {
    throw new Rejection(400, -32602, 'Unsupported tool call parameters');
  }
  const name = params.name;
  if (typeof name !== 'string' || !name) throw new Rejection(400, -32602, 'Tool name is required');
  if (!settings.clients[client].allowed_tools.includes(name)) {
    throw new Rejection(403, -32003, 'Tool is not permitted');
  }
  const args = Object.hasOwn(params, 'arguments') ? params.arguments : {};
  if (!isObject(args)) throw new Rejection(400, -32602, 'Tool arguments must be an object');
  if (!validators.get(name)(args)) {
    throw new Rejection(400, -32602, 'Tool arguments violate the configured schema');
  }
  const blocked = settings.tools[name].blocked_substrings.map(value => value.toLowerCase());
  const pending = [args];
  while (pending.length) {
    const value = pending.pop();
    if (isObject(value)) pending.push(...Object.keys(value), ...Object.values(value));
    else if (Array.isArray(value)) pending.push(...value);
    else if (typeof value === 'string' && blocked.some(text => value.toLowerCase().includes(text))) {
      throw new Rejection(403, -32003, 'Tool arguments contain prohibited text');
    }
  }
  return name;
}
