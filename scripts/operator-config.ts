import algosdk from 'algosdk';
import {AlgorandClient} from '@algorandfoundation/algokit-utils';
export function operatorChain(){
 const node=(kind:'ALGOD'|'INDEXER')=>{
  const value=process.env[`${kind}_URL`] || '',url=new URL(value);
  if(url.protocol!=='https:' && !(url.protocol==='http:'&&['127.0.0.1','localhost'].includes(url.hostname)))throw new Error('HTTPS or loopback provider required');
  const token=process.env[`${kind}_TOKEN`] || '';
  const headers:Record<string,string>=token?{[process.env[`${kind}_TOKEN_HEADER`] || (kind==='ALGOD'?'X-Algo-API-Token':'X-Indexer-API-Token')]:token}:{};
  return {url:value,headers};
 };
 const a=node('ALGOD'),i=node('INDEXER');
 return AlgorandClient.fromClients({algod:new algosdk.Algodv2(a.headers,a.url,''),indexer:new algosdk.Indexer(i.headers,i.url,'')});
}
export function operatorDatabase(){
 const origin=process.env.SUPABASE_URL || '',key=process.env.SUPABASE_SERVICE_ROLE_KEY || '';
 const url=new URL(origin);
 if(!key || (url.protocol!=='https:' && !(url.protocol==='http:'&&['127.0.0.1','localhost'].includes(url.hostname))))throw new Error('Configure the Supabase project and service credential');
 return async(path:string,method='GET',body?:unknown)=>{
  const response=await fetch(origin.replace(/\/$/,'')+path,{method,headers:{apikey:key,Authorization:`Bearer ${key}`,'Content-Type':'application/json',Prefer:'return=representation'},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(20000)});
  if(!response.ok)throw new Error(`Database operation failed (${response.status})`);
  return response.status===204?null:response.json();
 };
}
