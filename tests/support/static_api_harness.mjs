// Runs the static website's backtest "server" over a folder of exported data and prints the answers.
// Used by tests/test_static_api.py to compare it with the Python service:
//   node tests/support/static_api_harness.mjs <site folder> <queries.json>
// queries.json is a list of request URLs; the output is a list of {ok: answer} or {error: {...}}.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { staticGet } from "../../app/static/js/static-api.js";

const [root, queriesFile] = process.argv.slice(2);

globalThis.fetch = async (url) => {
  try {
    const text = readFileSync(join(root, url), "utf8");
    return { ok: true, status: 200, json: async () => JSON.parse(text) };
  } catch {
    return { ok: false, status: 404, json: async () => ({}) };
  }
};

const queries = JSON.parse(readFileSync(queriesFile, "utf8"));
const answers = [];
for (const url of queries) {
  try {
    answers.push({ ok: await staticGet(url) });
  } catch (error) {
    answers.push({ error: { message: error.message, code: error.code, status: error.status } });
  }
}
process.stdout.write(JSON.stringify(answers));
