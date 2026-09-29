import http from 'node:http';
import https from 'node:https';
import { Rejection } from './json.js';

/** Single fixed-destination request. No redirects, decompression, proxies, or retries. */
export function sendUpstream(url, { headers, body, maxBytes, timeoutMs }) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const request = (new URL(url).protocol === 'https:' ? https : http).request(url, {
      method: 'POST', headers, agent: false,
    });
    const timer = setTimeout(() => {
      const error = new Error('Upstream timeout');
      fail(error);
      request.destroy(error);
    }, timeoutMs);
    const fail = error => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(error);
    };
    request.on('error', fail);
    request.on('response', response => {
      const chunks = [];
      let size = 0;
      response.on('error', fail);
      response.on('data', chunk => {
        if (settled) return;
        size += chunk.length;
        if (size > maxBytes) {
          const error = new Rejection(502, -32002, 'Upstream response exceeds size limit');
          fail(error);
          request.destroy(error);
          return;
        }
        chunks.push(chunk);
      });
      response.on('end', () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve({ status: response.statusCode, headers: response.headers, body: Buffer.concat(chunks) });
      });
    });
    request.end(body);
  });
}
