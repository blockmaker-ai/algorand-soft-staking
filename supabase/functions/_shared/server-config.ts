import algosdk from 'algosdk';
export function networkIdentity(){
 const id=Deno.env.get('ALGORAND_GENESIS_ID') || '', hash=Deno.env.get('ALGORAND_GENESIS_HASH') || '';
 if(!id || !/^[A-Za-z0-9+/]{43}=$/.test(hash))throw new Error('Configure the expected Algorand network');
 return {id,hash};
}
export function provider(kind:'ALGOD'|'INDEXER'){
 const url=Deno.env.get(`${kind}_URL`) || '';
 const parsed=new URL(url);
 if(parsed.protocol!=='https:' && !(parsed.protocol==='http:' && ['127.0.0.1','localhost'].includes(parsed.hostname)))throw new Error('HTTPS or a loopback node is required');
 const token=Deno.env.get(`${kind}_TOKEN`) || '';
 const headers:Record<string,string>=token?{[Deno.env.get(`${kind}_TOKEN_HEADER`) || (kind==='ALGOD'?'X-Algo-API-Token':'X-Indexer-API-Token')]:token}:{};
 return {url:url.replace(/\/$/,''),headers};
}
export function chainClients(){
 const a=provider('ALGOD'),i=provider('INDEXER');
 return {algod:new algosdk.Algodv2(a.headers,a.url,''),indexer:new algosdk.Indexer(i.headers,i.url,'')};
}
