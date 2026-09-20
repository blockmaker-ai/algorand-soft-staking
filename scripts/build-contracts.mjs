import {execFileSync} from 'node:child_process';
import {mkdirSync,readFileSync,writeFileSync} from 'node:fs';
import {resolve} from 'node:path';
const bin=resolve('node_modules/.bin');
for(const [dir,name] of [['contracts/funded-rewards','FundedRewards'],['contracts/legacy','StakingPool']]){
  mkdirSync(`${dir}/artifacts`,{recursive:true});mkdirSync(`${dir}/client`,{recursive:true});
  execFileSync(`${bin}/puya-ts`,[`${name}.algo.ts`,'--out-dir','artifacts','--output-bytecode','--target-avm-version','11'],{cwd:dir,stdio:'inherit'});
  execFileSync(`${bin}/algokitgen`,['generate','-a',`${dir}/artifacts/${name}.arc56.json`,'-o',`${dir}/client/${name}Client.ts`],{stdio:'inherit'});
  const client=`${dir}/client/${name}Client.ts`;
  writeFileSync(client,readFileSync(client,'utf8').replace(/[ \t]+$/gm,'').trimEnd()+'\n');
}
