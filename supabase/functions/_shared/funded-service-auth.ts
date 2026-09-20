import {createHash,timingSafeEqual} from 'node:crypto';
export async function fundedServiceAuthorized(req:Request):Promise<boolean>{
 const expected=Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') || '';
 if(!expected)return false;
 const bearer=req.headers.get('authorization')?.match(/^Bearer (.+)$/)?.[1];
 const candidates=[req.headers.get('apikey'),bearer].filter((v):v is string=>!!v && v.length<=8192);
 const digest=(v:string)=>createHash('sha256').update(v).digest();
 return candidates.some(value=>timingSafeEqual(digest(value),digest(expected)));
}
