/**
 * Incremental parser for the text/event-stream format (WHATWG HTML, "Server-sent events").
 *
 * We read SSE through fetch instead of EventSource because EventSource cannot send
 * an Authorization header, and tokens must never go in URLs. That means parsing
 * the stream ourselves; this follows the spec's rules:
 *  - lines end in CRLF, LF or CR (a CR at the end of one chunk may pair with an LF at the start of the next);
 *  - a blank line dispatches the event; an event with no data lines is dropped;
 *  - "data" lines are joined with "\n"; one leading space after the colon is removed;
 *  - "id" sets the last event id (persisting across events) unless it contains NUL;
 *  - "retry" with an all-digit value is the reconnection delay hint;
 *  - lines starting with ":" are comments (heartbeats);
 *  - a leading UTF-8 BOM is ignored.
 */
export interface SseMessage {
  /** Last event id in effect when this message was dispatched. */
  id: string | undefined;
  /** "message" when no event field was given. */
  event: string;
  data: string;
}

export interface SseParser {
  push(chunk: string): void;
}

export function createSseParser(onMessage: (m: SseMessage) => void, onRetry?: (ms: number) => void): SseParser {
  let buffer = '';
  let first = true;
  let skipLeadingLf = false;
  let data: string[] = [];
  let eventName = '';
  let lastId: string | undefined;

  const dispatch = () => {
    if (data.length === 0) {
      eventName = '';
      return;
    }
    const message: SseMessage = { id: lastId, event: eventName || 'message', data: data.join('\n') };
    data = [];
    eventName = '';
    onMessage(message);
  };

  const processLine = (line: string) => {
    if (line === '') return dispatch();
    if (line.startsWith(':')) return; // comment / heartbeat
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    switch (field) {
      case 'event':
        eventName = value;
        break;
      case 'data':
        data.push(value);
        break;
      case 'id':
        if (!value.includes('\u0000')) lastId = value;
        break;
      case 'retry':
        if (/^\d+$/.test(value)) onRetry?.(Number(value));
        break;
      default:
        break; // unknown fields are ignored
    }
  };

  return {
    push(chunk: string) {
      if (first) {
        first = false;
        if (chunk.startsWith('﻿')) chunk = chunk.slice(1);
      }
      if (skipLeadingLf && chunk.startsWith('\n')) chunk = chunk.slice(1);
      skipLeadingLf = false;

      buffer += chunk;
      let start = 0;
      for (let i = 0; i < buffer.length; i++) {
        const c = buffer[i];
        if (c !== '\n' && c !== '\r') continue;
        processLine(buffer.slice(start, i));
        if (c === '\r') {
          if (i + 1 < buffer.length) {
            if (buffer[i + 1] === '\n') i++;
          } else {
            skipLeadingLf = true; // CR was the last char; an LF may follow in the next chunk
          }
        }
        start = i + 1;
      }
      buffer = buffer.slice(start);
    },
  };
}
