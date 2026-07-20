#!/usr/bin/env node
/**
 * Пушит ежемесячные дайджесты + разовые обзоры проектов
 * (connector/out/digest_payload.json, пишет connector.py --digest) в AFFiNE
 * как markdown-документы, разложенные по папкам сайдбара:
 *
 *   Дайджест проектов [NotebookLM]    (уже существует, создаётся ВРУЧНУЮ — если
 *    ├── Клиентские проекты           нет, скрипт останавливается с ошибкой,
 *    │    └── <notebook_name>         а не создаёт корень сам и не льёт
 *    │         ├── Обзор              документы без структуры)
 *    │         └── Журнал
 *    │              └── <Месяц Год>
 *    └── Внутренние проекты
 *         └── <notebook_name> ...
 *
 * «Обзор» создаётся один раз и больше не перезаписывается (connector.py не
 * знает, существует ли он уже в AFFiNE, поэтому спрашивает NotebookLM каждый
 * прогон — здесь лишний ответ просто отбрасывается, если документ уже есть).
 * Документ «Месяц Год» в «Журнале» — create-or-replace по каждому прогону.
 *
 * Пишет в AFFiNE не напрямую (это недокументированный Socket.IO/Yjs-протокол),
 * а поднимая уже проверенный на практике affine-mcp-server как короткоживущий
 * HTTP-процесс на loopback и вызывая его MCP-тулы (find_doc_by_title,
 * create_doc_from_markdown, replace_doc_with_markdown, list_organize_nodes,
 * create_folder, add_organize_link) — те же самые тулы, что уже работают для
 * существующего Obsidian→AFFiNE синка.
 *
 * Требуемые env: AFFINE_BASE_URL, AFFINE_API_TOKEN, AFFINE_WORKSPACE_ID.
 * affine-mcp-server должен быть установлен глобально (npm install -g
 * affine-mcp-server@<пиненная версия>) заранее — этот скрипт только его
 * запускает и останавливает.
 */
import { spawn } from "node:child_process";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import crypto from "node:crypto";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PAYLOAD_PATH = path.join(__dirname, "..", "out", "digest_payload.json");
const PORT = 3917;
const HTTP_TOKEN = crypto.randomBytes(24).toString("hex"); // только для этого процесса
const ROOT_FOLDER_NAME = "Дайджест проектов [NotebookLM]";

function requireEnv(name) {
  const v = process.env[name];
  if (!v) {
    console.error(`❌ Нет обязательной переменной окружения ${name}`);
    process.exit(1);
  }
  return v;
}

async function waitForHealthz(baseUrl, timeoutMs = 15000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const r = await fetch(`${baseUrl}/healthz`);
      if (r.ok) return;
    } catch {
      // ещё не поднялся — подождать и повторить
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  throw new Error(`affine-mcp-server не ответил на /healthz за ${timeoutMs}мс`);
}

function textOf(result) {
  return result.content?.[0]?.text ?? "{}";
}

function jsonOf(result) {
  return JSON.parse(textOf(result));
}

async function main() {
  const raw = await readFile(PAYLOAD_PATH, "utf-8").catch(() => "{}");
  const payload = JSON.parse(raw);
  const digests = Array.isArray(payload) ? payload : (payload.digests ?? []); // на всякий случай — совместимость со старым форматом (плоский массив)
  const overviews = Array.isArray(payload) ? [] : (payload.overviews ?? []);
  if (digests.length === 0 && overviews.length === 0) {
    console.log("Нет дайджестов/обзоров для пуша (пустой digest_payload.json) — выхожу.");
    return;
  }

  const AFFINE_BASE_URL = requireEnv("AFFINE_BASE_URL");
  const AFFINE_API_TOKEN = requireEnv("AFFINE_API_TOKEN");
  const AFFINE_WORKSPACE_ID = requireEnv("AFFINE_WORKSPACE_ID");

  console.log(`→ Запускаю affine-mcp-server (HTTP, порт ${PORT})...`);
  const child = spawn("affine-mcp", [], {
    env: {
      ...process.env,
      MCP_TRANSPORT: "http",
      PORT: String(PORT),
      AFFINE_BASE_URL,
      AFFINE_MCP_AUTH_MODE: "bearer",
      AFFINE_API_TOKEN,
      AFFINE_WORKSPACE_ID,
      AFFINE_MCP_HTTP_TOKEN: HTTP_TOKEN,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout.on("data", (d) => process.stdout.write(`  [affine-mcp] ${d}`));
  child.stderr.on("data", (d) => process.stderr.write(`  [affine-mcp] ${d}`));

  let exitCode = 0;
  try {
    const base = `http://127.0.0.1:${PORT}`;
    await waitForHealthz(base);

    const transport = new StreamableHTTPClientTransport(new URL(`${base}/mcp`), {
      requestInit: { headers: { Authorization: `Bearer ${HTTP_TOKEN}` } },
    });
    const client = new Client({ name: "b24-digest-push", version: "1.0.0" });
    await client.connect(transport);

    // ── Индекс организации сайдбара: один запрос, дальше держим в памяти и
    // дополняем локально по мере создания новых узлов (не дёргаем API на
    // каждую проверку — на 14 проектов это была бы куча лишних round-trip'ов).
    const nodes = jsonOf(await client.callTool({ name: "list_organize_nodes", arguments: {} })).nodes ?? [];

    const root = nodes.find((n) => n.type === "folder" && n.parentId === null && n.data === ROOT_FOLDER_NAME);
    if (!root) {
      console.error(
        `❌ Папка «${ROOT_FOLDER_NAME}» не найдена в корне сайдбара AFFiNE. ` +
        `Она создаётся вручную (не автоматически) — проверьте, не переименована/удалена ли. Останавливаюсь.`
      );
      process.exit(1);
    }

    async function findOrCreateFolder(name, parentId) {
      const existing = nodes.find((n) => n.type === "folder" && n.parentId === parentId && n.data === name);
      if (existing) return existing;
      const node = jsonOf(await client.callTool({ name: "create_folder", arguments: { name, parentId } }));
      nodes.push(node);
      return node;
    }

    async function findDocInFolder(title, parentId) {
      const found = await client.callTool({ name: "find_doc_by_title", arguments: { title } });
      const matches = jsonOf(found).matches ?? [];
      for (const m of matches) {
        if (nodes.some((n) => n.type === "doc" && n.parentId === parentId && n.data === m.id)) {
          return m.id;
        }
      }
      return null;
    }

    async function linkDoc(parentId, docId) {
      const node = jsonOf(await client.callTool({
        name: "add_organize_link",
        arguments: { folderId: parentId, type: "doc", targetId: docId },
      }));
      nodes.push(node);
    }

    // ── Сгруппировать дайджест + обзор по проекту (group_id) ────────────────
    const byProject = new Map();
    for (const d of digests) {
      if (!byProject.has(d.group_id)) {
        byProject.set(d.group_id, { notebook_name: d.notebook_name, project_type: d.project_type });
      }
      byProject.get(d.group_id).digest = d;
    }
    for (const o of overviews) {
      if (!byProject.has(o.group_id)) {
        byProject.set(o.group_id, { notebook_name: o.notebook_name, project_type: o.project_type });
      }
      byProject.get(o.group_id).overview = o;
    }

    for (const [groupId, p] of byProject) {
      const title = p.notebook_name;
      console.log(`→ [${title}] (${p.project_type ?? "client"})`);
      try {
        const catName = p.project_type === "internal" ? "Внутренние проекты" : "Клиентские проекты";
        const catFolder = await findOrCreateFolder(catName, root.id);
        const projFolder = await findOrCreateFolder(title, catFolder.id);

        if (p.overview) {
          const existingId = await findDocInFolder("Обзор", projFolder.id);
          if (existingId) {
            console.log(`  · [Обзор] уже существует (docId=${existingId}) — не перезаписываю`);
          } else {
            const created = await client.callTool({
              name: "create_doc_from_markdown",
              arguments: { title: "Обзор", markdown: p.overview.markdown },
            });
            const docId = jsonOf(created).docId;
            await linkDoc(projFolder.id, docId);
            console.log(`  ✅ [Обзор] создан (docId=${docId})`);
          }
        }

        if (p.digest) {
          const journalFolder = await findOrCreateFolder("Журнал", projFolder.id);
          const period = p.digest.period_ru;
          const existingId = await findDocInFolder(period, journalFolder.id);
          if (existingId) {
            await client.callTool({
              name: "replace_doc_with_markdown",
              arguments: { docId: existingId, markdown: p.digest.markdown },
            });
            console.log(`  ✅ [${period}] обновлён (docId=${existingId})`);
          } else {
            const created = await client.callTool({
              name: "create_doc_from_markdown",
              arguments: { title: period, markdown: p.digest.markdown },
            });
            const docId = jsonOf(created).docId;
            await linkDoc(journalFolder.id, docId);
            console.log(`  ✅ [${period}] создан (docId=${docId})`);
          }
        }
      } catch (e) {
        console.error(`  ❌ [${title}] не удалось запушить в AFFiNE: ${e}`);
        exitCode = 1;
      }
    }

    await client.close();
  } finally {
    child.kill("SIGTERM");
  }

  process.exit(exitCode);
}

main().catch((e) => {
  console.error("❌ affine_digest_push.mjs упал:", e);
  process.exit(1);
});
