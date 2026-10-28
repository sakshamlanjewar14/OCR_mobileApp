export const DEFAULT_EXPORT_BASE_NAME = "lead-gennie-export";
export const DEFAULT_EXPORT_FILE_NAME = "lead-gennie-export.xlsx";

export interface SanitizedFileNameResult {
  valid: boolean;
  error?: string;
  sanitizedBaseName: string;
  finalFileName: string;
}

/**
 * Validates and sanitizes a requested file name for Excel export.
 *
 * Rules:
 * - Accepts normal human-readable names (e.g., "October leads", "Pune business contacts", "Manager review 2026").
 * - Rejects empty or whitespace-only input unless allowDefaultFallback is explicitly requested.
 * - Strips control characters (0x00-0x1F, 0x7F).
 * - Removes path separators (/ and \) and path traversal attempts.
 * - Removes Windows/browser invalid characters: : * ? " < > |
 * - Strips trailing .xlsx extension(s) to avoid duplicate extensions like report.xlsx.xlsx.
 * - Collapses repeated spaces to a single space.
 * - Strips leading/trailing dots and spaces.
 * - Limits the final base name to 120 characters.
 * - Rejects names that consist only of invalid or special characters.
 * - Ensures exactly one .xlsx extension on the final file name.
 */
export function sanitizeExportFileName(
  input: string,
  allowDefaultFallback = false
): SanitizedFileNameResult {
  const trimmed = (input ?? "").trim();
  if (!trimmed) {
    if (allowDefaultFallback) {
      return {
        valid: true,
        sanitizedBaseName: DEFAULT_EXPORT_BASE_NAME,
        finalFileName: DEFAULT_EXPORT_FILE_NAME,
      };
    }
    return {
      valid: false,
      error: "Enter a file name.",
      sanitizedBaseName: "",
      finalFileName: "",
    };
  }

  // Remove control characters (0x00 - 0x1F, 0x7F)
  let cleaned = trimmed.replace(/[\x00-\x1f\x7f]/g, "");

  // Remove path separators (\ and /) and path traversal
  cleaned = cleaned.replace(/[/\\]/g, "");

  // Remove invalid Windows/browser file-name characters: : * ? " < > |
  cleaned = cleaned.replace(/[:*?"<>|]/g, "");

  // Strip any trailing .xlsx extension(s) the user may have entered (case-insensitive)
  cleaned = cleaned.replace(/(\.xlsx)+$/i, "");

  // Collapse repeated spaces to a single space
  cleaned = cleaned.replace(/\s+/g, " ").trim();

  // Strip leading/trailing dots and spaces
  cleaned = cleaned.replace(/^\.+|\.+$/g, "").trim();

  // Limit final base name to 120 characters
  cleaned = cleaned.slice(0, 120).trim();

  if (!cleaned) {
    return {
      valid: false,
      error: "File name cannot consist only of invalid or special characters.",
      sanitizedBaseName: "",
      finalFileName: "",
    };
  }

  return {
    valid: true,
    sanitizedBaseName: cleaned,
    finalFileName: `${cleaned}.xlsx`,
  };
}

export function getExportErrorMessage(status: number, defaultDetail?: string): string {
  if (status === 401 || status === 403) {
    return "Your session has expired. Please log in again.";
  }
  if (status === 404) {
    return "The export endpoint is unavailable.";
  }
  if (status === 500 || status === 503) {
    return "The server could not create the Excel file. Please try again.";
  }
  if (defaultDetail && typeof defaultDetail === "string" && defaultDetail.trim()) {
    return defaultDetail.trim();
  }
  return `Could not create spreadsheet (${status}).`;
}

export async function validateExportResponse(response: Response): Promise<Blob> {
  const contentType = (response.headers.get("content-type") || "").toLowerCase();

  if (!response.ok) {
    let detail = "";
    if (contentType.includes("application/json")) {
      try {
        const body = await response.json();
        detail = body?.detail || body?.error || body?.message || "";
      } catch {
        // ignore parse failure
      }
    }
    throw new Error(getExportErrorMessage(response.status, detail));
  }

  if (contentType.includes("text/html")) {
    throw new Error("The server returned an unexpected web page instead of an Excel file.");
  }

  if (contentType.includes("application/json")) {
    let detail = "";
    try {
      const body = await response.json();
      detail = body?.detail || body?.error || body?.message || "";
    } catch {
      // ignore parse failure
    }
    throw new Error(detail || "The server returned an unexpected error message instead of an Excel file.");
  }

  const blob = await response.blob();
  if (!blob || blob.size === 0) {
    throw new Error("Received an empty file from the server.");
  }

  const isXlsxMime = contentType.includes("spreadsheetml") || contentType.includes("openxmlformats");
  if (!isXlsxMime) {
    // If MIME is missing, generic octet-stream, etc., verify zip/PK magic bytes
    try {
      const buffer = await blob.slice(0, 4).arrayBuffer();
      const bytes = new Uint8Array(buffer);
      const isZip = bytes.length >= 2 && bytes[0] === 0x50 && bytes[1] === 0x4B;
      if (!isZip) {
        throw new Error("The response is not a valid Excel file.");
      }
    } catch (sliceErr) {
      if (sliceErr instanceof Error && sliceErr.message === "The response is not a valid Excel file.") {
        throw sliceErr;
      }
    }
  }

  return blob;
}

export function triggerBlobDownload(
  blob: Blob,
  fileName: string,
  doc?: Document,
  urlApi?: { createObjectURL: (b: Blob) => string; revokeObjectURL: (u: string) => void }
): boolean {
  try {
    const documentRef = doc || (typeof window !== "undefined" ? window.document : null);
    if (!documentRef) return false;

    const urlProvider = urlApi || (typeof URL !== "undefined" ? URL : null);
    const url = (urlProvider && typeof urlProvider.createObjectURL === "function")
      ? urlProvider.createObjectURL(blob)
      : "";
    if (!url) return false;

    const anchor = documentRef.createElement("a");
    anchor.href = url;
    anchor.download = fileName;
    anchor.rel = "noopener";
    documentRef.body.appendChild(anchor);
    anchor.click();
    anchor.remove();

    if (urlProvider && typeof urlProvider.revokeObjectURL === "function") {
      setTimeout(() => {
        try {
          urlProvider.revokeObjectURL(url);
        } catch {
          // ignore
        }
      }, 1000);
    }
    return true;
  } catch {
    return false;
  }
}

export interface ExportFlowOptions {
  blob: Blob;
  finalFileName: string;
  navigatorRef?: Navigator | null;
  documentRef?: Document | null;
  urlApi?: { createObjectURL: (b: Blob) => string; revokeObjectURL: (u: string) => void };
}

export interface ExportFlowResult {
  method: "share" | "download" | "manual";
  message: string;
}

export async function executeExportFlow({
  blob,
  finalFileName,
  navigatorRef = typeof navigator !== "undefined" ? navigator : null,
  documentRef = typeof document !== "undefined" ? document : null,
  urlApi,
}: ExportFlowOptions): Promise<ExportFlowResult> {
  let file: File | null = null;
  try {
    if (typeof File !== "undefined") {
      file = new File([blob], finalFileName, {
        type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      });
    }
  } catch {
    // File constructor unsupported or threw
    file = null;
  }

  const canShare = Boolean(
    navigatorRef &&
    typeof navigatorRef.share === "function" &&
    file &&
    (!navigatorRef.canShare || navigatorRef.canShare({ files: [file] }))
  );

  if (canShare && file && navigatorRef) {
    try {
      await navigatorRef.share({
        files: [file],
        title: finalFileName,
      });
      return {
        method: "share",
        message: `Spreadsheet ready: ${finalFileName}`,
      };
    } catch {
      // Catches NotAllowedError, AbortError, SecurityError, "Permission denied", user cancellation, etc.
      // Treat share rejection as a normal fallback condition and proceed to download
    }
  }

  // Automatic download fallback
  const downloadSuccess = triggerBlobDownload(blob, finalFileName, documentRef || undefined, urlApi);
  if (downloadSuccess) {
    return {
      method: "download",
      message: `Spreadsheet downloaded as ${finalFileName}.`,
    };
  }

  // If download could not be triggered (e.g. strict iOS Safari sandbox)
  return {
    method: "manual",
    message: "Excel file is ready. Use the browser share or download action to save it.",
  };
}
