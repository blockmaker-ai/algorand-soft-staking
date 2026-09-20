type Handler=(req:Request)=>Response|Promise<Response>;
async function endpoint(path:string){
 let handler:Handler|undefined;
 const original=Deno.serve;
 Deno.serve=((fn:Handler)=>{handler=fn;return {};}) as typeof Deno.serve;
 try{await import(path);}finally{Deno.serve=original;}
 if(!handler)throw new Error('Handler missing');return handler;
}
Deno.test('publisher rejects callers and remains gated without touching providers',async()=>{
 const original=Deno.env.get('SUPABASE_SERVICE_ROLE_KEY'),flag=Deno.env.get('FUNDED_REWARDS_PUBLICATION_ENABLED');
 try{
  Deno.env.set('SUPABASE_SERVICE_ROLE_KEY','fixture-service-credential');Deno.env.set('FUNDED_REWARDS_PUBLICATION_ENABLED','false');
  const handler=await endpoint('../../funded-publish/index.ts');
  const url='https://staking.example/functions/v1/funded-publish';
  if((await handler(new Request(url,{method:'POST',body:'{}'}))).status!==401)throw new Error('Anonymous publication accepted');
  const headers={Authorization:'Bearer fixture-service-credential','Content-Type':'application/json'};
  if((await handler(new Request(url,{method:'POST',headers,body:JSON.stringify({pool_id:'00000000-0000-4000-8000-000000000001',preview:false})}))).status!==409)throw new Error('Disabled publication accepted');
  if((await handler(new Request(url,{method:'POST',headers,body:'{"pool_id":"../unsafe"}'}))).status!==400)throw new Error('Invalid identity accepted');
 }finally{
  if(original===undefined)Deno.env.delete('SUPABASE_SERVICE_ROLE_KEY');else Deno.env.set('SUPABASE_SERVICE_ROLE_KEY',original);
  if(flag===undefined)Deno.env.delete('FUNDED_REWARDS_PUBLICATION_ENABLED');else Deno.env.set('FUNDED_REWARDS_PUBLICATION_ENABLED',flag);
 }
});
