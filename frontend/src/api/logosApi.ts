// A client's reference logos -- the real brand marks, attached to the
// keyword they belong to (backend/api/logos.py).
//
// WHAT THEY DO. A profile discovered under a keyword has its cached avatar
// compared against that keyword's marks, and a hit lifts it to the top of
// triage with a "logo match" badge. It is what catches the impersonator
// wearing the right logo under a name that scores badly -- the case keyword
// matching alone ranks near the bottom.
//
// Entirely optional: a client with no logos behaves exactly as before.
import { json, url } from "./httpClient";

export interface ClientLogo {
  id: string;
  client_id: string;
  /** The parent keyword this mark belongs to. Empty = applies to all of them. */
  keyword: string;
  kind: string;
  sha: string;
  phash: string;
  dhash: string;
  width: number;
  height: number;
  bytes: number;
  filename: string;
  content_type: string;
}

/** Where to render a stored reference. Content-addressed, so it never changes. */
export function logoImageUrl(clientId: string, logoId: string): string {
  return url(`/clients/${encodeURIComponent(clientId)}/logos/${encodeURIComponent(logoId)}/image`);
}

export const logosApi = {
  list: (clientId: string) =>
    fetch(url(`/clients/${encodeURIComponent(clientId)}/logos`)).then(
      json<{ items: ClientLogo[] }>,
    ),

  // multipart, not JSON: the image goes up as bytes rather than base64,
  // which would inflate it by a third for no benefit.
  upload: (clientId: string, file: File, keyword: string, kind: string) => {
    const body = new FormData();
    body.append("file", file);
    body.append("keyword", keyword);
    body.append("kind", kind);
    return fetch(url(`/clients/${encodeURIComponent(clientId)}/logos`), {
      method: "POST",
      body, // no Content-Type header -- the browser sets the multipart boundary
    }).then(json<ClientLogo>);
  },

  remove: (clientId: string, logoId: string) =>
    fetch(url(`/clients/${encodeURIComponent(clientId)}/logos/${encodeURIComponent(logoId)}`), {
      method: "DELETE",
    }).then(json<ClientLogo>),
};
