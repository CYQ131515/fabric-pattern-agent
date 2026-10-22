import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
let sharp;
for (const spec of [process.env.SHARP_PATH, 'sharp']) {
  if (!spec) continue;
  try { sharp = require(spec); break; } catch {}
}
if (!sharp) throw new Error('未找到 sharp：请在当前目录执行 npm install，或设置 SHARP_PATH');
export default sharp;
