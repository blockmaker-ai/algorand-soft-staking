import {createHash} from 'node:crypto';
import {gzipSync} from 'node:zlib';
import {verifyArchivedEvidence} from '../funded-evidence.ts';
const assert=(v:unknown)=>{if(!v)throw new Error('assertion failed');};
const raw=new TextEncoder().encode('{"large_uint":18446744073709551615,"name":"\\u00e9"}');
const hash=createHash('sha256').update(raw).digest('hex');
const response=(data:Uint8Array=gzipSync(raw))=>new Response(new Uint8Array(data));
async function rejects(fn:()=>Promise<unknown>){let failed=false;try{await fn();}catch{failed=true;}assert(failed);}

Deno.test('archive verification hashes exact Python bytes without rounding large JSON integers',async()=>{
  await verifyArchivedEvidence(response(),hash);
});
Deno.test('missing, changed and truncated evidence archives are rejected',async()=>{
  await rejects(()=>verifyArchivedEvidence(new Response('',{status:404}),hash));
  await rejects(()=>verifyArchivedEvidence(response(gzipSync('{}')),hash));
  await rejects(()=>verifyArchivedEvidence(response(gzipSync(raw).slice(0,10)),hash));
});
Deno.test('compressed and expanded evidence streams are both bounded',async()=>{
  await rejects(()=>verifyArchivedEvidence(response(),hash,8));
  const large=new TextEncoder().encode('x'.repeat(10000));
  await rejects(()=>verifyArchivedEvidence(response(gzipSync(large)),createHash('sha256').update(large).digest('hex'),100));
});
