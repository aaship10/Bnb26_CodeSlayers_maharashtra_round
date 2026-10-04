// Validates individual chart dataset files (experiment runner output) against D's chartSchema.
//   frontend/node_modules/.bin/tsx simulator/tests/contract/chart_file.check.ts <chart.json>...
import { readFileSync } from 'node:fs';
import { chartSchema } from '../../../frontend/src/features/sim/schemas';

let failed = 0;
for (const f of process.argv.slice(2)) {
  const r = chartSchema.safeParse(JSON.parse(readFileSync(f, 'utf8')));
  if (r.success) {
    console.log(`OK   ${f}`);
  } else {
    failed++;
    const issues = r.error.issues.slice(0, 5).map((i) => `${i.path.join('.')}: ${i.message}`);
    console.log(`FAIL ${f}\n     ${issues.join('\n     ')}`);
  }
}
process.exit(failed ? 1 : 0);
