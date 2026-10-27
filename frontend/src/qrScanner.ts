/** Keep waiting for camera frames, and stop all work once decoded or closed. */
export function startQrScanLoop(options: {
  ready: () => boolean;
  nativeScan?: () => Promise<string | undefined>;
  fallbackScan: () => string | undefined;
  onResult: (value: string) => void;
}): () => void {
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const scan = async () => {
    if (stopped) return;
    try {
      if (options.ready()) {
        let value: string | undefined;
        try { value = await options.nativeScan?.(); } catch { /* Use local decoder. */ }
        if (stopped) return;
        value ||= options.fallbackScan();
        if (value?.trim()) {
          stopped = true;
          options.onResult(value.trim());
          return;
        }
      }
    } catch { /* Retry frames while the camera settles. */ }
    if (!stopped) timer = setTimeout(() => void scan(), 250);
  };
  void scan();
  return () => { stopped = true; if (timer !== undefined) clearTimeout(timer); };
}
