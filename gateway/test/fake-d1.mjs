// D1 faked with node:sqlite running the real migrations (shared by the gateway tests).
import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";

const MIGRATIONS = new URL("../migrations/", import.meta.url);

export class FakeD1 {
  constructor() {
    this.db = new DatabaseSync(":memory:");
    for (const f of readdirSync(MIGRATIONS).sort()) this.db.exec(readFileSync(new URL(f, MIGRATIONS), "utf8"));
  }
  async batch(stmts) {
    // D1 runs a batch as one transaction.
    this.db.exec("BEGIN");
    try {
      const out = [];
      for (const s of stmts) out.push(await s.run());
      this.db.exec("COMMIT");
      return out;
    } catch (e) {
      this.db.exec("ROLLBACK");
      throw e;
    }
  }
  prepare(sql) {
    const db = this.db;
    const stmt = {
      args: [],
      bind(...a) { stmt.args = a; return stmt; },
      async first() { return db.prepare(sql).get(...stmt.args) ?? null; },
      async run() { const r = db.prepare(sql).run(...stmt.args); return { success: true, meta: { changes: Number(r.changes) } }; },
      async all() { return { results: db.prepare(sql).all(...stmt.args) }; },
    };
    return stmt;
  }
}
