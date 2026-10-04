// Validates what the /sim service returns against D's zod schemas (frontend/src/features/sim/schemas.ts).
// Run by tests/test_service.py via the frontend's tsx:
//   frontend/node_modules/.bin/tsx simulator/tests/contract/service_payloads.check.ts <dir>
// <dir> holds JSON captured from the live service: scenarios.json, run.json, runs.json,
// results.json, experiments.json, chart.json, snapshot.json (the last SSE snapshot payload).
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { z } from '../../../frontend/node_modules/zod/index.js';
import {
  chartSchema,
  experimentSchema,
  resultsSchema,
  runListSchema,
  runSchema,
  scenarioSchema,
  snapshotSchema,
} from '../../../frontend/src/features/sim/schemas';

type Schema = { safeParse: (d: unknown) => { success: boolean; error?: { issues: { path: (string | number)[]; message: string }[] } } };

const dir = process.argv[2];
let failed = 0;

function check(file: string, schema: Schema): void {
  const path = join(dir, file);
  if (!existsSync(path)) {
    failed++;
    console.log(`MISSING ${file}`);
    return;
  }
  const r = schema.safeParse(JSON.parse(readFileSync(path, 'utf8')));
  if (r.success) {
    console.log(`OK   ${file}`);
    return;
  }
  failed++;
  const issues = (r.error?.issues ?? []).slice(0, 6).map((i) => `${i.path.join('.')}: ${i.message}`);
  console.log(`FAIL ${file}\n     ${issues.join('\n     ')}`);
}

check('scenarios.json', z.array(scenarioSchema));
check('run.json', runSchema);
check('runs.json', runListSchema);
check('results.json', resultsSchema);
check('experiments.json', z.array(experimentSchema));
check('chart.json', chartSchema);
check('snapshot.json', snapshotSchema);

process.exit(failed ? 1 : 0);
