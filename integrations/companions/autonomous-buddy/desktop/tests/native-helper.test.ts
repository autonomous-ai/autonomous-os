import { afterEach, expect, it } from 'vitest'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { NativeHelper } from '../src/main/native-helper'

const roots: string[] = []
const helpers: NativeHelper[] = []
afterEach(async () => {
  await Promise.all(helpers.splice(0).map((helper) => helper.stop()))
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })))
})
async function fixture(onMenuAction: (action: 'open-manager' | 'quit') => void = () => {}) {
  const root = await mkdtemp(join(tmpdir(), 'buddy-native-ipc-'))
  roots.push(root)
  const script = join(root, 'helper.mjs')
  await writeFile(
    script,
    `import {createInterface} from 'node:readline';
const emit = value => process.stdout.write(JSON.stringify(value)+'\\n');
let paused = false;
console.log('native diagnostic, not a protocol frame');
const state = () => ({paired:false,paused,connection:'disconnected',accessibility:false,screenRecording:false,devices:[]});
emit({event:'state',state:state()});
for await (const line of createInterface({input:process.stdin})) {
const {id,method,params}=JSON.parse(line);
if(method==='status') emit({id,result:state()});
else if(method==='pause'){paused=params.paused;emit({event:'state',state:state()});emit({id,result:state()});}
else if(method==='command') {
if(params.action==='menu') {emit({event:'menu',action:params.params.action});emit({id,result:{ok:true}});continue;}
if(params.action==='crash'){process.exit(3)}
else if(params.action==='error') emit({id,error:'native rejected'});
else setTimeout(()=>emit({id,result:{id,ok:true,result:{value:params.params.value,pid:process.pid}}}),params.params.delay||0);
}
}
`,
  )
  const helper = new NativeHelper(process.execPath, () => {}, [script], onMenuAction)
  helpers.push(helper)
  helper.start()
  return helper
}
it('correlates out-of-order native responses and state events over private pipes', async () => {
  const helper = await fixture()
  expect((await helper.status()).available).toBe(true)
  const [a, b] = await Promise.all([
    helper.command('ping', { value: 'a', delay: 40 }),
    helper.command('ping', { value: 'b' }),
  ])
  expect(a.result?.value).toBe('a')
  expect(b.result?.value).toBe('b')
  await helper.action('pause', { paused: true })
  expect((await helper.status()).paused).toBe(true)
  await expect(helper.command('error')).rejects.toThrow('native rejected')
})
it('rejects pending requests on native crash and allows explicit restart', async () => {
  const helper = await fixture()
  await expect(helper.command('crash')).rejects.toThrow('stopped')
  expect((await helper.status()).available).toBe(false)
  await helper.action('restart')
  expect((await helper.status()).available).toBe(true)
})
it('closes stdin and reaps the helper on owner shutdown', async () => {
  const helper = await fixture()
  const pid = (await helper.command('ping')).result?.pid as number
  await helper.stop()
  expect(() => process.kill(pid, 0)).toThrow()
  expect((await helper.status()).available).toBe(false)
})

it('routes only supported native menu actions to the owning app', async () => {
  const actions: string[] = []
  const helper = await fixture((action) => actions.push(action))
  await helper.command('menu', { action: 'open-manager' })
  await helper.command('menu', { action: 'unsupported' })
  await helper.command('menu', { action: 'quit' })
  expect(actions).toEqual(['open-manager', 'quit'])
})
