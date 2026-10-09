// Requests to Pig, each with a timeout, each answering one question: what happened?
//
// Every function here resolves with a plain object describing the outcome. None of them
// throws for anything the server or the network does, because the sender has to decide
// what to do about every outcome, and a thrown error is too easy to treat as "try again".
//
// The outcomes:
//
//   { ok: true, status, body }           the server did what was asked
//   { ok: false, retry: true, ... }      the network failed, or the server had a problem
//                                        of its own (5xx); the same request may work later
//   { ok: false, retry: false, ... }     the server refused; sending it again won't help
//
// A 409 (closed run) and a 422 (some events refused) also come back with their body,
// because the body says what to do next.

import type { TaskParameters } from "./types.ts";

export const DEFAULT_TIMEOUT_MS = 15_000;

// `body` is whatever JSON the server answered with. Each caller knows, from docs/api.md,
// which shape to expect from the request it made.
export type Outcome =
  | { ok: true; status: number; body: any }
  | { ok: false; retry: boolean; status: number; body?: any; message: string };

export type Http = ReturnType<typeof makeHttp>;

export function makeHttp({
  fetch = globalThis.fetch.bind(globalThis),
  XHR = globalThis.XMLHttpRequest,
  timeoutMs = DEFAULT_TIMEOUT_MS,
}: { fetch?: typeof globalThis.fetch; XHR?: typeof XMLHttpRequest; timeoutMs?: number } = {}) {
  async function request(method: string, url: string, body?: string | Blob, waitMs = timeoutMs): Promise<Outcome> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), waitMs);
    let response: Response;
    try {
      response = await fetch(url, {
        method,
        headers: typeof body === "string" ? { "Content-Type": "application/json" } : {},
        body,
        signal: controller.signal,
      });
    } catch (error) {
      // Offline, DNS, refused connection, CORS, or our own timeout. All look the same
      // from here, and all are worth trying again.
      const timedOut = controller.signal.aborted;
      return {
        ok: false,
        retry: true,
        status: 0,
        message: timedOut ? `No answer from ${url} in ${waitMs} ms.` : `Couldn't reach ${url}: ${(error as Error).message}`,
      };
    } finally {
      clearTimeout(timer);
    }

    return outcome(method, url, response.status, parseJson(await response.text()));
  }

  /**
   * PUT a media part with XMLHttpRequest, because fetch can't say how much of an upload
   * has gone. `onProgress` gets the bytes sent so far. The timeout counts from the last
   * sign of progress rather than from the start, since a big part on a slow connection
   * can take minutes and still be fine.
   */
  function upload(url: string, blob: Blob, onProgress?: (sentBytes: number) => void): Promise<Outcome> {
    if (XHR === undefined) return request("PUT", url, blob); // Node, in the tests.
    return new Promise((resolve) => {
      const xhr = new XHR();
      let timer: ReturnType<typeof setTimeout> | undefined;
      const stillGoing = () => {
        clearTimeout(timer);
        timer = setTimeout(() => xhr.abort(), timeoutMs);
      };
      const done = (result: Outcome) => {
        clearTimeout(timer);
        resolve(result);
      };
      xhr.open("PUT", url);
      xhr.upload.onprogress = (event) => {
        stillGoing();
        onProgress?.(event.loaded);
      };
      xhr.onprogress = stillGoing;
      xhr.onload = () => done(outcome("PUT", url, xhr.status, parseJson(xhr.responseText)));
      xhr.onerror = () => done({ ok: false, retry: true, status: 0, message: `Couldn't reach ${url}.` });
      xhr.onabort = () =>
        done({ ok: false, retry: true, status: 0, message: `${url} stopped answering for ${timeoutMs} ms.` });
      stillGoing();
      xhr.send(blob);
    });
  }

  return {
    /**
     * GET /task/{task}. With `waitMs`, gives up sooner than usual: a participant is
     * waiting on this one, and saved settings will do if the network is slow.
     */
    taskSettings(server: string, task: string, waitMs?: number) {
      return request("GET", `${base(server)}/task/${encodeURIComponent(task)}`, undefined, waitMs);
    },

    /** POST /task/{task}/run. `waitMs` as for taskSettings. */
    startRun(server: string, task: string, parameters: TaskParameters, waitMs?: number) {
      return request(
        "POST",
        `${base(server)}/task/${encodeURIComponent(task)}/run`,
        JSON.stringify(parameters),
        waitMs,
      );
    },

    /**
     * POST /task/{task}/run/{run_id}, with a body already turned into JSON.
     * The body is built from the exact strings stored in the queue, so a retry sends
     * the same bytes as the first try did.
     */
    sendEvents(server: string, task: string, runId: string, jsonBody: string) {
      return request("POST", runUrl(server, task, runId), jsonBody);
    },

    /** POST /task/{task}/run/{run_id}/media, with the body already turned into JSON. */
    startMedia(server: string, task: string, runId: string, jsonBody: string) {
      return request("POST", `${runUrl(server, task, runId)}/media`, jsonBody);
    },

    /** PUT /task/{task}/run/{run_id}/media/{media_id}/{part} */
    sendPart(
      server: string,
      task: string,
      runId: string,
      mediaId: number,
      part: number,
      blob: Blob,
      onProgress?: (sentBytes: number) => void,
    ) {
      return upload(`${runUrl(server, task, runId)}/media/${mediaId}/${part}`, blob, onProgress);
    },

    /** POST /task/{task}/run/{run_id}/media/{media_id}/finish */
    finishMedia(server: string, task: string, runId: string, mediaId: number, parts: number) {
      return request("POST", `${runUrl(server, task, runId)}/media/${mediaId}/finish`, JSON.stringify({ parts }));
    },

    /** POST /task/{task}/run/{run_id}/finalize */
    finalize(server: string, task: string, runId: string) {
      return request("POST", `${runUrl(server, task, runId)}/finalize`);
    },
  };
}

function base(server: string): string {
  return server.replace(/\/+$/, "");
}

function runUrl(server: string, task: string, runId: string): string {
  return `${base(server)}/task/${encodeURIComponent(task)}/run/${encodeURIComponent(runId)}`;
}

/** What a response comes to, by its status code. */
function outcome(method: string, url: string, status: number, body: any): Outcome {
  if (status >= 200 && status < 300) return { ok: true, status, body };
  // A refusal of one event or part says why under `errors`, not at the top.
  const firstError = Object.values(body?.errors ?? {})[0] as { message?: string } | undefined;
  const message = body?.message ?? firstError?.message ?? `${method} ${url} answered ${status}.`;
  if (status >= 500 || status === 408 || status === 429) {
    return { ok: false, retry: true, status, body, message };
  }
  return { ok: false, retry: false, status, body, message };
}

function parseJson(text: string): any {
  try {
    return JSON.parse(text);
  } catch {
    return undefined; // A proxy's HTML error page, say.
  }
}
