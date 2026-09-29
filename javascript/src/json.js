import { TextDecoder } from 'node:util';

export const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);
export const has = (value, key) => Object.hasOwn(value, key);

export class Rejection extends Error {
  constructor(status, code, message, retryAfter) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
  }
}

export function checkDepth(value, maximum) {
  const pending = [[value, 0]];
  while (pending.length) {
    const [node, depth] = pending.pop();
    if (depth > maximum) throw new Rejection(400, -32602, 'JSON nesting limit exceeded');
    if (node && typeof node === 'object') {
      for (const child of Object.values(node)) pending.push([child, depth + 1]);
    }
  }
}

export function strictJson(raw) {
  const text = new TextDecoder('utf-8', { fatal: true }).decode(raw);
  const result = JSON.parse(text);
  // JSON.parse validates syntax but silently overwrites duplicate keys. Scan the
  // validated source to reject duplicate (including escape-equivalent) names.
  const objects = [];
  for (let i = 0; i < text.length; i++) {
    if (text[i] === '{') objects.push(new Set());
    else if (text[i] === '[') objects.push(null);
    else if (text[i] === '}' || text[i] === ']') objects.pop();
    else if (text[i] === '"') {
      const start = i++;
      while (text[i] !== '"') {
        if (text[i] === '\\') i++;
        i++;
      }
      const value = JSON.parse(text.slice(start, i + 1));
      if (!value.isWellFormed()) throw new Error('Invalid Unicode');
      let next = i + 1;
      while (/\s/.test(text[next] ?? '') && next < text.length) next++;
      if (text[next] === ':') {
        const keys = objects.at(-1);
        if (keys.has(value)) throw new Error('Duplicate JSON key');
        keys.add(value);
      }
    }
  }
  const pending = [result];
  while (pending.length) {
    const value = pending.pop();
    if (typeof value === 'number' && (!Number.isFinite(value) ||
        (Number.isInteger(value) && !Number.isSafeInteger(value)))) {
      throw new Error('Non-finite or unsafe JSON number');
    }
    if (value && typeof value === 'object') pending.push(...Object.values(value));
  }
  return result;
}
