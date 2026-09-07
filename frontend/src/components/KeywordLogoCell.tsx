// The optional brand mark attached to one keyword, in the keyword grid.
//
// WHY IT LIVES HERE, next to the keyword rather than on a settings page:
// the mark and the keyword are one piece of configuration. "These are the
// terms I search for this brand, and this is what that brand looks like."
// Splitting them would leave an analyst maintaining two lists that have to
// agree, and the one that quietly falls out of date would be the one that
// silently stops catching impersonators.
//
// UPLOAD NEEDS A SAVED CLIENT. A logo is stored server-side against a
// client id, so there is nothing to attach it to until the client exists.
// Rather than fail the upload after the analyst has picked a file, the cell
// says so up front while the client is still unsaved.
import { useRef, useState } from "react";
import toast from "react-hot-toast";
import { logoImageUrl, logosApi, type ClientLogo } from "../api/logosApi";

interface Props {
  clientId: string;
  keyword: string;
  kind: "individual" | "domain";
  /** Every logo already stored for this client; this cell picks out its own. */
  logos: ClientLogo[];
  onChanged: () => void;
  disabled?: boolean;
}

export function KeywordLogoCell({ clientId, keyword, kind, logos, onChanged, disabled }: Props) {
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [busy, setBusy] = useState(false);

  const kw = keyword.trim().toLowerCase();
  const mine = logos.filter((l) => (l.keyword || "").trim().toLowerCase() === kw);

  const pick = () => fileRef.current?.click();

  const onFile = async (file: File | undefined) => {
    if (!file) return;
    if (!clientId) {
      toast("Save the client first, then attach a logo.", { icon: "💾" });
      return;
    }
    setBusy(true);
    try {
      await logosApi.upload(clientId, file, keyword, kind);
      toast.success(`Logo attached to "${keyword}"`);
      onChanged();
    } catch (e) {
      // The backend refuses a file it cannot decode rather than storing it,
      // precisely so this never becomes configured protection that does
      // nothing -- so its message is worth showing verbatim.
      toast.error((e as Error).message || "Could not attach that image");
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const remove = async (logo: ClientLogo) => {
    setBusy(true);
    try {
      await logosApi.remove(clientId, logo.id);
      toast.success("Logo removed");
      onChanged();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" }}>
      {mine.map((logo) => (
        <span key={logo.id} style={{ position: "relative", display: "inline-flex" }}>
          <img
            src={logoImageUrl(clientId, logo.id)}
            alt={logo.filename || "reference logo"}
            title={`${logo.filename || "logo"} — ${logo.width}x${logo.height}`}
            style={{
              width: 28, height: 28, objectFit: "contain", borderRadius: "6px",
              border: "1px solid var(--border-color)", background: "var(--bg-inner)",
            }}
          />
          <button
            type="button"
            onClick={() => void remove(logo)}
            disabled={disabled || busy}
            title="Remove this reference logo"
            style={{
              position: "absolute", top: -6, right: -6, width: 16, height: 16,
              borderRadius: "50%", border: "none", cursor: "pointer", lineHeight: "16px",
              background: "var(--danger, #e95053)", color: "#fff", fontSize: "10px", padding: 0,
            }}
          >
            ×
          </button>
        </span>
      ))}

      <input
        ref={fileRef}
        type="file"
        accept="image/png,image/jpeg,image/webp"
        style={{ display: "none" }}
        onChange={(e) => void onFile(e.target.files?.[0])}
      />
      <button
        type="button"
        onClick={pick}
        disabled={disabled || busy || !keyword.trim()}
        title={
          clientId
            ? "Attach the real brand logo for this keyword. Profiles found under it "
              + "whose picture matches will be flagged and sorted to the top."
            : "Save the client first, then attach a logo"
        }
        style={{
          padding: "3px 8px", borderRadius: "6px", cursor: clientId ? "pointer" : "not-allowed",
          border: "1px dashed var(--border-color)", background: "transparent",
          color: "var(--text-dim)", fontSize: "10px", fontWeight: 600, opacity: busy ? 0.5 : 1,
        }}
      >
        {busy ? "…" : mine.length ? "+ logo" : "＋ logo"}
      </button>
    </div>
  );
}
