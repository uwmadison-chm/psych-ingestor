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

export const DEFAULT_TIMEOUT_MS = 15_000;

/**
 * @param {object} options
 * @param {typeof fetch} [options.fetch]
 * @param {number} [options.timeoutMs]
 */
export function makeHttp({ fetch = globalThis.fetch.bind(globalThis), timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  async function request(method, url, body, waitMs = timeoutMs) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), waitMs);
    let response;
    try {
      response = await fetch(url, {
        method,
        headers: body === undefined ? {} : { "Content-Type": "application/json" },
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
        message: timedOut ? `No answer from ${url} in ${waitMs} ms.` : `Couldn't reach ${url}: ${error.message}`,
      };
    } finally {
      clearTimeout(timer);
    }

    let parsed;
    try {
      parsed = await response.json();
    } catch {
      parsed = undefined; // A proxy's HTML error page, say.
    }
    const status = response.status;
    if (status >= 200 && status < 300) return { ok: true, status, body: parsed };
    const message = parsed?.message ?? `${method} ${url} answered ${status}.`;
    if (status >= 500 || status === 408 || status === 429) {
      return { ok: false, retry: true, status, body: parsed, message };
    }
    return { ok: false, retry: false, status, body: parsed, message };
  }

  return {
    /**
     * GET /task/{task}. With `waitMs`, gives up sooner than usual: a participant is
     * waiting on this one, and saved settings will do if the network is slow.
     */
    taskSettings(server, task, waitMs) {
      return request("GET", `${base(server)}/task/${encodeURIComponent(task)}`, undefined, waitMs);
    },

    /** POST /task/{task}/run. `waitMs` as for taskSettings. */
    startRun(server, task, parameters, waitMs) {
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
    sendEvents(server, task, runId, jsonBody) {
      return request(
        "POST",
        `${base(server)}/task/${encodeURIComponent(task)}/run/${encodeURIComponent(runId)}`,
        jsonBody,
      );
    },

    /** POST /task/{task}/run/{run_id}/finalize */
    finalize(server, task, runId) {
      return request(
        "POST",
        `${base(server)}/task/${encodeURIComponent(task)}/run/${encodeURIComponent(runId)}/finalize`,
      );
    },
  };
}

function base(server) {
  return server.replace(/\/+$/, "");
}
