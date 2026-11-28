// Independently verify the operator's exact signed approvals for retained proofs.
import { createPublicKey, verify } from 'node:crypto';
import { closeSync, openSync, readSync } from 'node:fs';

const limit = 256 * 1024;
const schema = 'noah-runtime-approvals/1';
const hash = /^0x[0-9a-f]{64}$/;
const sha = /^[0-9a-f]{64}$/;
const uint = /^(0|[1-9][0-9]{0,9})$/;
const capabilities = new Set(['burn', 'ema', 'reports', 'token', 'weights']);
const spkiPrefix = Buffer.from('302a300506032b6570032100', 'hex');

function load(path: string, key: string): string[][] {
  const buffer = Buffer.alloc(limit + 1);
  const fd = openSync(path, 'r');
  let size = 0;
  try {
    while (size < buffer.length) {
      const count = readSync(fd, buffer, size, buffer.length - size, null);
      if (!count) break;
      size += count;
    }
  } finally {
    closeSync(fd);
  }
  if (size > limit || buffer[128] !== 10) throw new Error('invalid runtime approval envelope');
  const signature = buffer.subarray(0, 128).toString('latin1');
  const payload = buffer.subarray(129, size);
  if (!/^[0-9a-f]{128}$/.test(signature)) throw new Error('invalid runtime signature encoding');
  const publicKey = createPublicKey({
    key: Buffer.concat([spkiPrefix, Buffer.from(key, 'hex')]), format: 'der', type: 'spki',
  });
  if (!verify(null, payload, publicKey, Buffer.from(signature, 'hex'))) {
    throw new Error('invalid runtime approval signature');
  }
  const lines = payload.toString('latin1').split('\n');
  if (lines.shift() !== schema || lines.pop() !== '' || !lines.length || lines.length > 1024) {
    throw new Error('invalid runtime approval schema');
  }
  let previousGenesis = '';
  let previousSpec = -1;
  return lines.map(line => {
    const row = line.split('\t');
    const [genesis, spec, transaction, code, metadata, source, rawCapabilities] = row;
    const caps = rawCapabilities?.split(',') ?? [];
    if (row.length !== 7 || !hash.test(genesis) || !hash.test(code) || !sha.test(metadata)
        || !/^[0-9a-f]{40}$/.test(source) || !uint.test(spec) || !uint.test(transaction)
        || Math.max(Number(spec), Number(transaction)) >= 2 ** 32
        || genesis < previousGenesis || genesis === previousGenesis && Number(spec) <= previousSpec
        || caps.some((name, index) => !capabilities.has(name) || index > 0 && name <= caps[index - 1])
        || caps.includes('ema') && !caps.includes('token')) {
      throw new Error('invalid runtime approval row');
    }
    previousGenesis = genesis;
    previousSpec = Number(spec);
    return row;
  });
}

export function approvedMetadata(
  genesis: string, spec: number, transaction: number, code: string, capability: string,
): string | undefined {
  const path = process.env.NOAH_RUNTIME_APPROVALS;
  const key = process.env.NOAH_RUNTIME_APPROVAL_KEY;
  if (!path || !key || !sha.test(key)) return undefined;
  try {
    return load(path, key).find(row => row[0] === genesis && Number(row[1]) === spec
      && Number(row[2]) === transaction && row[3] === code && row[6].split(',').includes(capability))?.[4];
  } catch {
    return undefined;
  }
}
