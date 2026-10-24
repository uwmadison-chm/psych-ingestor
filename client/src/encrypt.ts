// End-to-end encryption, for a task whose settings carry a public key.
//
// Events and media parts are encrypted here, in the worker, as they're queued. What's
// stored on the device and sent is the ciphertext, so a lost device holds nothing
// readable, and a retry sends exactly the bytes it sent before. Encrypting again on
// retry would give different bytes, which the server reads as a different event with
// the same ID. See psych-ingestor issue #10.
//
// The encryption is age (https://age-encryption.org), by its author's own library. The
// private key is only on the secure side. Nothing here can decrypt anything.

import { Encrypter } from "age-encryption";

/**
 * What age adds to a file, besides the 16 bytes per 64 KiB chunk: its header for one
 * public key, and the nonce. Always the same for one X25519 key, so a media part can be
 * cut to fit the server's limit exactly once it's encrypted.
 */
const HEADER_BYTES = 184;
const CHUNK_BYTES = 64 * 1024;
const TAG_BYTES = 16;

/** How big a file of `plaintext` bytes is once it's encrypted. */
export function encryptedSize(plaintext: number): number {
  return plaintext + HEADER_BYTES + TAG_BYTES * Math.max(1, Math.ceil(plaintext / CHUNK_BYTES));
}

/** The most plaintext that fits in `limit` bytes once encrypted. 0 if nothing does. */
export function plaintextThatFits(limit: number): number {
  let size = Math.max(0, limit - HEADER_BYTES - TAG_BYTES * Math.ceil(limit / CHUNK_BYTES));
  while (encryptedSize(size + 1) <= limit) size += 1;
  while (size > 0 && encryptedSize(size) > limit) size -= 1;
  return encryptedSize(size) <= limit ? size : 0;
}

/** Encrypt bytes to one public key. */
export async function encryptBytes(publicKey: string, plaintext: Uint8Array): Promise<Uint8Array> {
  const encrypter = new Encrypter();
  encrypter.addRecipient(publicKey);
  return encrypter.encrypt(plaintext);
}

/**
 * An event's `data` for an encrypted task: the JSON of what the task recorded,
 * encrypted, as base64. The server checks it's an age file and stores it as it is.
 */
export async function encryptedData(publicKey: string, data: unknown): Promise<string> {
  const ciphertext = await encryptBytes(publicKey, new TextEncoder().encode(JSON.stringify(data)));
  return base64(ciphertext);
}

function base64(bytes: Uint8Array): string {
  // btoa takes a string of single-byte characters. Built a piece at a time, because
  // spreading a big array into String.fromCharCode overflows the call stack.
  let text = "";
  for (let start = 0; start < bytes.length; start += 0x8000) {
    text += String.fromCharCode(...bytes.subarray(start, start + 0x8000));
  }
  return btoa(text);
}
