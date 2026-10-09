// The little that both the page and the worker need.

/** An error from the client. `code` says which kind; `message` is for people. */
export class PigError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "PigError";
    this.code = code;
  }
}

/** The local time with its UTC offset, like 2026-10-02T14:03:11.482-05:00. */
export function wallTime(date = new Date()) {
  const pad = (n, width = 2) => String(Math.abs(n)).padStart(width, "0");
  const offset = -date.getTimezoneOffset();
  const sign = offset >= 0 ? "+" : "-";
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}` +
    `.${pad(date.getMilliseconds(), 3)}` +
    `${sign}${pad(Math.floor(Math.abs(offset) / 60))}:${pad(Math.abs(offset) % 60)}`
  );
}
