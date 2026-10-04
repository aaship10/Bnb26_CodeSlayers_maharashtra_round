// Validates simulator output files against D's zod schemas (frontend/src/features/sim/schemas.ts).
// Run by tests/test_frontend_contract.py via the frontend's tsx:
//   frontend/node_modules/.bin/tsx simulator/tests/contract/frontend_schemas.check.ts <sample_results dir>
// Prints one line per file; exit code 1 if any file is rejected.
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { chartSchema, experimentSchema, resultsSchema } from '../../../frontend/src/features/sim/schemas';

type Schema = { safeParse: (d: unknown) => { success: boolean; error?: { issues: { path: (string | number)[]; message: string }[] } } };

const root = process.argv[2];
let failed = 0;

function check(label: string, schema: Schema, data: unknown): void {
  const r = schema.safeParse(data);
  if (r.success) {
    console.log(`OK   ${label}`);
    return;
  }
  failed++;
  const issues = (r.error?.issues ?? []).slice(0, 5).map((i) => `${i.path.join('.')}: ${i.message}`);
  console.log(`FAIL ${label}\n     ${issues.join('\n     ')}`);
}

const read = (p: string): unknown => JSON.parse(readFileSync(p, 'utf8'));

for (const f of readdirSync(join(root, 'results'))) check(`results/${f}`, resultsSchema, read(join(root, 'results', f)));
for (const f of readdirSync(join(root, 'charts'))) check(`charts/${f}`, chartSchema, read(join(root, 'charts', f)));
const exps = read(join(root, 'experiments.json')) as unknown[];
exps.forEach((e, i) => check(`experiments.json[${i}]`, experimentSchema, e));

process.exit(failed ? 1 : 0);
