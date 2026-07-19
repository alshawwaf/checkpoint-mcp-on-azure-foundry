#!/usr/bin/env node
// Minimal MCP stdio probe: spawns a @chkp server exactly the way the agent
// does (npx -y <package@version>, TELEMETRY_DISABLED=true), then drives
// initialize -> initialized -> tools/list (prints the tool names) and an
// optional tools/call. Raw JSON-RPC frames are printed in both directions so
// you can see precisely what the wire carries.
// Usage: node scripts/mcp_probe.mjs <package@version> [tool] [json-args]
//   e.g. node scripts/mcp_probe.mjs @chkp/quantum-management-mcp@1.4.7 show_hosts '{"limit": 5}'
// Servers that need extra startup flags take them via CHKP_ARGS (the same
// convention the AWS server containers used), e.g.:
//        CHKP_ARGS="--region US" node scripts/mcp_probe.mjs @chkp/documentation-mcp@1.4.6
import { spawn } from 'node:child_process';

const pkg = process.argv[2];
if (!pkg) {
  console.error('usage: node scripts/mcp_probe.mjs <package@version> [tool] [json-args]');
  process.exit(2);
}
const callTool = process.argv[3] || null;
const callArgs = callTool ? JSON.parse(process.argv[4] || '{}') : null;
const serverArgs = (process.env.CHKP_ARGS || '').split(' ').filter(Boolean);

const child = spawn('npx', ['-y', pkg, ...serverArgs], {
  stdio: ['pipe', 'pipe', 'pipe'],
  env: { ...process.env, TELEMETRY_DISABLED: 'true' },
});

let buf = '';
const pending = new Map();
let nextId = 1;
const send = (method, params) => {
  const id = nextId++;
  const msg = { jsonrpc: '2.0', id, method, ...(params ? { params } : {}) };
  console.log('>>> ' + JSON.stringify(msg));
  child.stdin.write(JSON.stringify(msg) + '\n');
  return new Promise((res) => pending.set(id, res));
};
const notify = (method, params) => {
  const msg = { jsonrpc: '2.0', method, ...(params ? { params } : {}) };
  console.log('>>> ' + JSON.stringify(msg));
  child.stdin.write(JSON.stringify(msg) + '\n');
};

child.stdout.on('data', (d) => {
  buf += d.toString();
  let nl;
  while ((nl = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, nl).trim();
    buf = buf.slice(nl + 1);
    if (!line) continue;
    let msg;
    try { msg = JSON.parse(line); } catch { continue; } // ignore non-JSON log lines
    console.log('<<< ' + line);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  }
});

const stderrLines = [];
child.stderr.on('data', (d) => stderrLines.push(d.toString()));
child.on('error', (e) => { console.error('SPAWN ERROR:', e.message); process.exit(1); });

const bail = setTimeout(() => {
  console.error('TIMEOUT after 90s. stderr tail:\n' + stderrLines.join('').slice(-1500));
  child.kill('SIGKILL');
  process.exit(1);
}, 90000);

(async () => {
  const init = await send('initialize', {
    protocolVersion: '2025-06-18',
    capabilities: {},
    clientInfo: { name: 'chkpmcpaz-mcp-probe', version: '0.1.0' },
  });
  notify('notifications/initialized');
  if (init.error) {
    console.error('initialize failed:', JSON.stringify(init.error));
    clearTimeout(bail);
    child.kill('SIGTERM');
    process.exit(1);
  }

  const list = await send('tools/list', {});
  const tools = list.result?.tools ?? [];
  console.log(`\n${tools.length} tools:`);
  for (const t of tools) console.log(`  ${t.name}`);

  if (callTool) {
    const r = await send('tools/call', { name: callTool, arguments: callArgs });
    console.log('\ntools/call result:');
    console.log(JSON.stringify(r.result ?? r.error, null, 2).slice(0, 3000));
  }

  clearTimeout(bail);
  child.kill('SIGTERM');
  process.exit(0);
})();
