// Private-fixture replay on an in-process PostgreSQL database. No network or cloud credentials.
import {readdir, readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {createInterface} from 'node:readline';
import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';

const {database, rpc, seed} = await import(pathToFileURL(resolve(
  'src/lib/database/test-support/acquisition-binding.mjs')).href);
async function databaseThrough(version, expectedCount) {
  const require = createRequire(resolve('package.json'));
  const {PGlite} = require('@electric-sql/pglite');
  const files = (await readdir('supabase/migrations')).filter(file =>
    file.endsWith('.sql') && file.split('_')[0] <= version).sort();
  if (files.length !== expectedCount || files.at(-1)?.split('_')[0] !== version) {
    throw new Error('Local migration files do not match the recorded production baseline');
  }
  const db = new PGlite();
  await db.exec('create role anon;create role authenticated;create role service_role bypassrls;');
  for (const file of files) await db.exec(await readFile('supabase/migrations/' + file, 'utf8'));
  return db;
}

let db;
for await (const line of createInterface({input: process.stdin})) {
  try {
    const {op, ...request} = JSON.parse(line);
    if (op === 'close') {
      await db?.close();
      process.stdout.write(JSON.stringify({result: true}) + '\n', () => process.exit(0));
      break;
    }
    let result;
    if (op === 'init') {
      db = request.migrationVersion
        ? await databaseThrough(request.migrationVersion, request.migrationCount)
        : await database();
      await seed(db, request.seed);
      result = true;
    } else if (op === 'rpc') {
      result = await rpc(db, request.name, request.body);
    } else if (op === 'inspect') {
      result = {
        manifests: (await db.query('select payload from analysis_input_manifests')).rows,
        preparations: (await db.query('select request from capability_acquisition_preparations')).rows,
        recipes: (await db.query('select recipe from capability_generation_recipes')).rows,
        credits: (await db.query('select credit_balance from users')).rows,
        ledger: (await db.query('select kind,delta from credit_ledger')).rows,
      };
    } else if (op === 'query') {
      result = (await db.query(request.sql, request.parameters)).rows;
    } else {
      throw new Error('Unknown A3 test operation');
    }
    process.stdout.write(JSON.stringify({result}) + '\n');
  } catch (error) {
    process.stdout.write(JSON.stringify({error: error.message, code: error.code ?? null}) + '\n');
  }
}
