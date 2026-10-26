export type LeadDraft = {
  id: string;
  userId: string;
  fields: Record<string, unknown>;
  updatedAt: number;
};

const DB_NAME = "lead-gennie-local";
const STORE_NAME = "drafts";
const QUEUE_STORE = "sync_queue";

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 2);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(STORE_NAME)) {
        database.createObjectStore(STORE_NAME, { keyPath: "id" });
      }
      if (!database.objectStoreNames.contains(QUEUE_STORE)) database.createObjectStore(QUEUE_STORE, { keyPath: "id" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("Local draft storage is unavailable."));
  });
}

export type SyncItem = { id: string; userId: string; idempotencyKey: string; payload: Record<string, unknown>; updatedAt: number };

export async function enqueueLead(userId: string, payload: Record<string, unknown>, idempotencyKey: string): Promise<void> {
  if (!userId || typeof indexedDB === "undefined") return;
  const database = await openDatabase();
  await new Promise<void>((resolve, reject) => {
    const transaction = database.transaction(QUEUE_STORE, "readwrite");
    transaction.objectStore(QUEUE_STORE).put({ id: `lead:${userId}:${idempotencyKey}`, userId, payload, idempotencyKey, updatedAt: Date.now() } satisfies SyncItem);
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error || new Error("Could not queue the lead locally."));
  });
  database.close();
}

export async function readLeadQueue(userId: string): Promise<SyncItem[]> {
  if (!userId || typeof indexedDB === "undefined") return [];
  const database = await openDatabase();
  const items = await new Promise<SyncItem[]>((resolve, reject) => {
    const request = database.transaction(QUEUE_STORE, "readonly").objectStore(QUEUE_STORE).getAll();
    request.onsuccess = () => resolve((request.result as SyncItem[]).filter((item) => item.userId === userId));
    request.onerror = () => reject(request.error || new Error("Could not read the local sync queue."));
  });
  database.close();
  return items;
}

export async function removeQueuedLead(id: string): Promise<void> {
  if (typeof indexedDB === "undefined") return;
  const database = await openDatabase();
  await new Promise<void>((resolve, reject) => {
    const transaction = database.transaction(QUEUE_STORE, "readwrite");
    transaction.objectStore(QUEUE_STORE).delete(id);
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error || new Error("Could not remove the synced lead."));
  });
  database.close();
}

export async function saveLeadDraft(userId: string, fields: Record<string, unknown>): Promise<void> {
  if (!userId || typeof indexedDB === "undefined") return;
  const database = await openDatabase();
  await new Promise<void>((resolve, reject) => {
    const transaction = database.transaction(STORE_NAME, "readwrite");
    transaction.objectStore(STORE_NAME).put({ id: `draft:${userId}`, userId, fields, updatedAt: Date.now() } satisfies LeadDraft);
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error || new Error("Could not save the local draft."));
  });
  database.close();
}

export async function readLeadDraft(userId: string): Promise<LeadDraft | null> {
  if (!userId || typeof indexedDB === "undefined") return null;
  const database = await openDatabase();
  const draft = await new Promise<LeadDraft | null>((resolve, reject) => {
    const request = database.transaction(STORE_NAME, "readonly").objectStore(STORE_NAME).get(`draft:${userId}`);
    request.onsuccess = () => resolve((request.result as LeadDraft | undefined) || null);
    request.onerror = () => reject(request.error || new Error("Could not read the local draft."));
  });
  database.close();
  return draft;
}

export async function clearLeadDraft(userId: string): Promise<void> {
  if (!userId || typeof indexedDB === "undefined") return;
  const database = await openDatabase();
  await new Promise<void>((resolve, reject) => {
    const transaction = database.transaction(STORE_NAME, "readwrite");
    transaction.objectStore(STORE_NAME).delete(`draft:${userId}`);
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error || new Error("Could not clear the local draft."));
  });
  database.close();
}
