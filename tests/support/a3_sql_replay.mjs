// Private-fixture replay on an in-process PostgreSQL database. No network or cloud credentials.
import {createInterface} from 'node:readline';
import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';

const {database, rpc, seed} = await import(pathToFileURL(resolve(
  'src/lib/database/test-support/acquisition-binding.mjs')).href);
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
      db = await database();
      await seed(db, request.seed);
      result = true;
    } else if (op === 'rpc') {
      result = await rpc(db, request.name, request.body);
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
