import { buildApp } from './app';

const PORT = Number(process.env.MOCK_PORT ?? 8787);
const HOST = process.env.MOCK_HOST ?? '127.0.0.1';

const { app, world } = buildApp();

app.addHook('onResponse', async (req, reply) => {
  if (req.url.startsWith('/__mock/state')) return; // the dev panel polls this
  console.log(`${req.method.padEnd(5)} ${String(reply.statusCode)} ${req.url}`);
});

app
  .listen({ port: PORT, host: HOST })
  .then(() => {
    console.log(`\n  Fair Drop mock server on http://${HOST}:${PORT}`);
    console.log(`  scenario: ${world.scenarioId} | mock time: ${world.clock.iso()}`);
    console.log('  OTP for every sign-up: 123456\n');
  })
  .catch((err) => {
    console.error(err);
    process.exit(1);
  });
