import {createHash} from 'node:crypto';

/** Verify the exact canonical Python JSON bytes without parsing huge account
 * histories in the Edge runtime. The scheduler separately checks the archived
 * calculation against every sealed credit. Hashing is streamed and bounded.
 */
export async function verifyArchivedEvidence(response:Response,expected:string,limit=64*1024*1024) {
  if(!/^[0-9a-f]{64}$/.test(expected) || !response.ok || !response.body)throw new Error('Reward evidence archive is unavailable');
  if(Number(response.headers.get('content-length') || '0')>limit)throw new Error('Reward evidence exceeds the size limit');
  let compressed=0,expanded=0;
  const limited=response.body.pipeThrough(new TransformStream<Uint8Array,BufferSource>({
    transform(chunk,controller){compressed+=chunk.length;if(compressed>limit)throw new Error('Reward archive exceeds the size limit');controller.enqueue(new Uint8Array(chunk));},
  }));
  const reader=limited.pipeThrough(new DecompressionStream('gzip')).getReader();
  const hash=createHash('sha256');
  try {
    while(true){
      const {done,value}=await reader.read();
      if(done)break;
      expanded+=value.length;
      if(expanded>limit)throw new Error('Reward evidence exceeds the size limit');
      hash.update(value);
    }
    if(hash.digest('hex')!==expected)throw new Error('Reward evidence checksum differs from the sealed batch');
  } finally {await reader.cancel().catch(()=>{});reader.releaseLock();}
}
