// Local interoperability fixture; the backend is never connected.
import { createServer } from 'node:http';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
const dir = mkdtempSync(join(tmpdir(), 'harness-direct-test-'));
process.env.ADAPTER_DATA_DIR = dir;
const base = process.env.HARNESS_CLI_ROOT + '/';
if (!process.env.HARNESS_CLI_ROOT)
    throw new Error('HARNESS_CLI_ROOT is required');
const { BackendSocket } = await import(base + 'src/backendSocket.ts');
const { AutonomousDeviceService } = await import(base + 'src/lib/autonomous-device/service.ts');
const { AutonomousDeviceDirect } = await import(base + 'src/lib/autonomous-device/direct.ts');
const { b64d, fingerprint } = await import(base + 'src/lib/e2ee/core.ts');
const { default: Bonjour } = await import(base + 'node_modules/bonjour-service/dist/index.js');
const backend = new BackendSocket('interop-machine'); // Never call connect(): backend remains offline.
let submits = 0;
const service = new AutonomousDeviceService({ machineId: 'interop-machine', serverInstanceId: 'interop-instance', agents: () => [{ agentId: 'agent-one', name: 'Test agent', engine: 'claude', state: 'idle' }], submit: (id, text, deliveryId) => { submits++; service.delivery({ sessionId: id, deliveryId, state: 'delivered' }); }, cancelDelivery: () => true, stop: async () => true, answer: async () => true, recent: () => [], emit: f => backend.emitAutonomousDeviceEvent(f) });
backend.setAutonomousDeviceService(service);
const host = {
    machineId: backend.machineId, label: 'Interop Mac',
    receive: (id, f, p) => backend.receiveDirectDevice(id, f, p), attach: (id, send) => backend.attachDirectDevice(id, send), detach: id => backend.detachDirectDevice(id),
    pending: () => backend.pendingPair(), pendingConnection: () => backend.e2ee.pendingConnection(),
    authenticatedFingerprint: id => { const pub = backend.e2ee.sessionIdentity(id); return pub ? fingerprint(b64d(pub)) : null; },
    pairedFingerprint: id => backend.pairedDirectFingerprint(id), pair: code => backend.pair(code), paired: () => backend.listPairs(),
};
let direct = new AutonomousDeviceDirect(host, dir);
backend.onDirectDeviceRevoked = fp => direct.revoked(fp);
const bonjour = new Bonjour();
let published: any;
const server = createServer(async (req, res) => {
    try {
        let raw = '';
        for await (const b of req)
            raw += b;
        const body = raw ? JSON.parse(raw) : {};
        let out: any;
        switch (req.url) {
            case '/advertise':
                published = bonjour.publish({ name: 'Harness Interop ' + process.pid, type: 'autonomous', port: body.port });
                out = { name: published.name };
                break;
            case '/discover':
                out = { devices: await direct.discover() };
                break;
            case '/pair':
                try {
                    out = await direct.pair(body.id, body.code);
                }
                catch (e) {
                    out = { error: e.code ?? e.message };
                }
                break;
            case '/restart':
                direct.stop();
                await new Promise(r => setTimeout(r, 100));
                direct = new AutonomousDeviceDirect(host, dir);
                direct.start();
                out = { ok: true };
                break;
            case '/revoke':
                out = backend.revoke(body.fingerprint);
                break;
            case '/state':
                out = { submits, pairs: backend.listPairs(), backend: backend.isConnected(), commander: backend.autonomousDeviceConnected() };
                break;
            default: out = { error: 'bad route' };
        }
        res.end(JSON.stringify(out));
    }
    catch (e) {
        res.statusCode = 500;
        res.end(JSON.stringify({ error: e.message }));
    }
});
server.listen(0, '127.0.0.1', () => process.stdout.write(JSON.stringify({ port: server.address().port }) + '\n'));
const cleanup = () => { direct.stop(); bonjour.destroy(); rmSync(dir, { recursive: true, force: true }); process.exit(0); };
process.on('SIGTERM', cleanup);
process.on('SIGINT', cleanup);
