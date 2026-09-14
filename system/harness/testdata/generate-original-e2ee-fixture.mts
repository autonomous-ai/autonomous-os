// Public, deterministic interoperability vectors from the unmodified Harness core.
// HARNESS_CORE_PATH points at cli/src/lib/e2ee/core.ts in a Harness checkout.
import { writeFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
const C = await import(pathToFileURL(process.env.HARNESS_CORE_PATH!).href)
const rng = (start: number) => (n: number) => Uint8Array.from({ length: n }, (_, i) => (start + i) & 255)
const machineId = 'original-device-relay-vector'
const adapter = C.newIdentity(rng(1)), device = C.newIdentity(rng(33))
const aEph = C.newEphemeral(rng(65)), dEph = C.newEphemeral(rng(97))
const keys = C.sessionKeys(dEph.priv, aEph.pub, machineId, dEph.pub, aEph.pub)
const groupKey = rng(129)(32), nextGroupKey = rng(161)(32), epoch = 'epoch-one'
const hello = { type: 'e2e_hello', payload: { identityPub: C.b64e(device.pub), ephPub: C.b64e(dEph.pub), sig: C.b64e(C.helloSig(device.priv, machineId, dEph.pub)) } }
const welcome = { type: 'e2e_welcome', payload: { webEphPub: C.b64e(dEph.pub), ephPub: C.b64e(aEph.pub), sig: C.b64e(C.welcomeSig(adapter.priv, machineId, dEph.pub, aEph.pub)), enc: C.b64e(C.aeadSeal(keys.s2c, 0, C.utf8('e2e-welcome'), C.utf8(JSON.stringify({ groupKey: C.b64e(groupKey), epoch })))) } }
const requestPayload = { type: 'hello', requestId: 'test-request', proto: 1 }
const request = { type: 'autonomous_device_request', payload: C.wrapPayload(keys.c2s, 'p', 0, 'autonomous_device_request', undefined, requestPayload) }
const resultPayload = { type: 'hello_result', requestId: 'test-request', proto: 1, machineId }
const result = { type: 'autonomous_device_result', payload: C.wrapPayload(keys.s2c, 'p', 1, 'autonomous_device_result', undefined, resultPayload) }
const commanderPayload = { kind: 'summary', text: 'Xin chào', agentId: 'agent-1' }
const commander = { type: 'commander_event', dbSessionId: 'session-1', payload: C.wrapPayload(groupKey, 'g', 0, 'commander_event', 'session-1', commanderPayload, epoch) }
const rekey = { type: 'e2e_rekey', payload: { n: 2, enc: C.b64e(C.aeadSeal(keys.s2c, 2, C.utf8('e2e-rekey'), C.utf8(JSON.stringify({ groupKey: C.b64e(nextGroupKey), epoch: 'epoch-two' })))) } }
const rotated = { type: 'commander_event', dbSessionId: 'session-1', payload: C.wrapPayload(nextGroupKey, 'g', 0, 'commander_event', 'session-1', commanderPayload, 'epoch-two') }
const pairId = rng(193)(16), ci = C.pairContext(machineId, 'device'), generator = C.cpaceGenerator('K7P4X9', pairId, ci)
writeFileSync(new URL('./original-e2ee-protocol.json', import.meta.url), JSON.stringify({ v: 1, note: 'Public test-only keys; original Harness core, device role.', machineId, deviceSeed: C.b64e(device.priv), adapterPub: C.b64e(adapter.pub), deviceEphPriv: C.b64e(dEph.priv), c2s: C.b64e(keys.c2s), s2c: C.b64e(keys.s2c), pair: { code: 'K7P4X9', pairId: C.b64e(pairId), ci, generator: C.b64e(generator.toRawBytes()) }, hello, welcome, requestPayload, request, resultPayload, result, commanderPayload, commander, rekey, rotated }, null, 2) + '\n')
