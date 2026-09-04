// chatImageStore — best-effort IndexedDB persistence for chat image
// attachments, keyed by "<ChatMessage id>#<index>".
//
// The index is why the key is composite: one message can carry SEVERAL photos
// (the composer accepts a multi-select), and a plain message-id key could only
// ever hold the last one. Everything that addresses a message — delete, prune —
// therefore compares the BASE id, not the raw key.
//
// Why: chat history lives in localStorage (~5MB origin quota), so
// saveConvos() strips `imageUrl` data-URLs before persisting — a few photos
// would blow the quota. That made attached images vanish on reload. IndexedDB
// has a far larger quota (hundreds of MB), so the blobs live here and the
// ChatSection mount effect re-attaches them to the loaded messages.
//
// Every call is best-effort: IndexedDB can be unavailable (private browsing,
// storage pressure) and images are a nice-to-have — a failure must never
// break chat, so all errors resolve to a harmless empty result.

const DB_NAME = "os-chat-images";
const DB_VERSION = 1;
const STORE = "images";

// Entries younger than this are never pruned, even when their message id is
// not in the keep-set — closes the race where an image is written for a
// message that hasn't reached the localStorage snapshot the keep-set was
// built from yet.
const PRUNE_MIN_AGE_MS = 60_000;

interface StoredImage {
  dataUrl: string;
  savedAt: number;
}

function openDB(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === "undefined") {
      reject(new Error("indexedDB unavailable"));
      return;
    }
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      if (!req.result.objectStoreNames.contains(STORE)) {
        req.result.createObjectStore(STORE);
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error("indexedDB open failed"));
  });
}

// Run one readwrite transaction; resolves when it commits. Best-effort.
async function withStore(mode: IDBTransactionMode, fn: (store: IDBObjectStore) => void): Promise<void> {
  const db = await openDB();
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE, mode);
      fn(tx.objectStore(STORE));
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error ?? new Error("indexedDB tx failed"));
      tx.onabort = () => reject(tx.error ?? new Error("indexedDB tx aborted"));
    });
  } finally {
    db.close();
  }
}

/** Strip the "#<index>" suffix so message-addressed calls still match. */
function baseMessageId(key: string): string {
  const hash = key.lastIndexOf("#");
  return hash === -1 ? key : key.slice(0, hash);
}

/** Persist a message's image data-URLs, in order. Fire-and-forget from send. */
export async function putChatImages(messageId: string, dataUrls: string[]): Promise<void> {
  if (dataUrls.length === 0) return;
  try {
    await withStore("readwrite", (store) => {
      const savedAt = Date.now();
      dataUrls.forEach((dataUrl, i) => {
        const entry: StoredImage = { dataUrl, savedAt };
        store.put(entry, `${messageId}#${i}`);
      });
    });
  } catch {
    /* best-effort */
  }
}

/**
 * All stored images as messageId → dataUrls, in attachment order. Used once at
 * mount to rehydrate. Grouped by base id so a message that carried several
 * photos comes back with all of them, not just whichever key sorted last.
 */
export async function getAllChatImages(): Promise<Map<string, string[]>> {
  const out = new Map<string, string[]>();
  try {
    const db = await openDB();
    try {
      await new Promise<void>((resolve, reject) => {
        const tx = db.transaction(STORE, "readonly");
        const store = tx.objectStore(STORE);
        const keysReq = store.getAllKeys();
        const valsReq = store.getAll();
        tx.oncomplete = () => {
          const keys = keysReq.result as string[];
          const vals = valsReq.result as StoredImage[];
          // getAllKeys is sorted, so "id#0" precedes "id#1" and pushing in
          // iteration order preserves the order they were attached in.
          keys.forEach((k, i) => {
            const dataUrl = vals[i]?.dataUrl;
            if (!dataUrl) return;
            const id = baseMessageId(k);
            const list = out.get(id);
            if (list) list.push(dataUrl);
            else out.set(id, [dataUrl]);
          });
          resolve();
        };
        tx.onerror = () => reject(tx.error ?? new Error("indexedDB read failed"));
      });
    } finally {
      db.close();
    }
  } catch {
    /* best-effort */
  }
  return out;
}

/** Delete the images of specific messages (e.g. a deleted conversation). */
export async function deleteChatImages(messageIds: string[]): Promise<void> {
  if (messageIds.length === 0) return;
  try {
    const doomed = new Set(messageIds);
    const db = await openDB();
    try {
      await new Promise<void>((resolve, reject) => {
        const tx = db.transaction(STORE, "readwrite");
        const store = tx.objectStore(STORE);
        // Cursor rather than delete-by-key: one message owns several keys now,
        // and only the base id is known here.
        const cursorReq = store.openCursor();
        cursorReq.onsuccess = () => {
          const cursor = cursorReq.result;
          if (!cursor) return;
          if (doomed.has(baseMessageId(cursor.key as string))) cursor.delete();
          cursor.continue();
        };
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error ?? new Error("indexedDB tx failed"));
        tx.onabort = () => reject(tx.error ?? new Error("indexedDB tx aborted"));
      });
    } finally {
      db.close();
    }
  } catch {
    /* best-effort */
  }
}

/**
 * Drop entries whose message id is no longer in any stored conversation —
 * messages trimmed by MAX_MESSAGES/MAX_CONVOS or history dropped by TTL.
 * Entries younger than PRUNE_MIN_AGE_MS are kept regardless (see above).
 */
export async function pruneChatImages(keepIds: Set<string>): Promise<void> {
  try {
    const db = await openDB();
    try {
      await new Promise<void>((resolve, reject) => {
        const tx = db.transaction(STORE, "readwrite");
        const store = tx.objectStore(STORE);
        const cursorReq = store.openCursor();
        const cutoff = Date.now() - PRUNE_MIN_AGE_MS;
        cursorReq.onsuccess = () => {
          const cursor = cursorReq.result;
          if (!cursor) return;
          const key = cursor.key as string;
          const val = cursor.value as StoredImage;
          if (!keepIds.has(baseMessageId(key)) && (val?.savedAt ?? 0) < cutoff) {
            cursor.delete();
          }
          cursor.continue();
        };
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error ?? new Error("indexedDB prune failed"));
      });
    } finally {
      db.close();
    }
  } catch {
    /* best-effort */
  }
}

/** Wipe everything — paired with clearLocalChatHistory(). */
export async function clearChatImages(): Promise<void> {
  try {
    await withStore("readwrite", (store) => {
      store.clear();
    });
  } catch {
    /* best-effort */
  }
}
