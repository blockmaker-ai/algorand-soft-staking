import {fundedServiceAuthorized} from '../funded-service-auth.ts';
Deno.test('publication accepts only the configured service credential',async()=>{
 const original=Deno.env.get('SUPABASE_SERVICE_ROLE_KEY');
 try{
  Deno.env.set('SUPABASE_SERVICE_ROLE_KEY','fixture-service-credential');
  for(const value of ['', 'anonymous-credential', 'forged.jwt.claims']){
   if(await fundedServiceAuthorized(new Request('https://staking.example',{headers:{Authorization:`Bearer ${value}`}})))throw new Error('Unexpected access');
  }
  if(!await fundedServiceAuthorized(new Request('https://staking.example',{headers:{Authorization:'Bearer fixture-service-credential'}})))throw new Error('Expected service access');
 }finally{if(original===undefined)Deno.env.delete('SUPABASE_SERVICE_ROLE_KEY');else Deno.env.set('SUPABASE_SERVICE_ROLE_KEY',original);}
});
