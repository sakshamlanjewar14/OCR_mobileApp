/** Keep waiting for camera frames, and stop all work once decoded or closed. */
export function startQrScanLoop(options: {
  ready: () => boolean;
  nativeScan?: () => Promise<string | string[] | undefined>;
  fallbackScan: () => string | string[] | undefined;
  onResult: (value: string) => void;
  onResults?: (values: string[]) => void;
}): () => void {
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let nativeTimer: ReturnType<typeof setTimeout> | undefined;
  let nativeScan = options.nativeScan;
  const scan = async () => {
    if (stopped) return;
    try {
      if (options.ready()) {
        let value: string | string[] | undefined;
        if (nativeScan) {
          try {
            value = await Promise.race([nativeScan(), new Promise<undefined>(resolve => {
              nativeTimer = setTimeout(() => { nativeScan = undefined; resolve(undefined); }, 1500);
            })]);
          } catch { nativeScan = undefined; }
          finally { if (nativeTimer !== undefined) clearTimeout(nativeTimer); }
        }
        if (stopped) return;
        if (!value || (Array.isArray(value) && !value.length)) value = options.fallbackScan();
        const values = [...new Set((Array.isArray(value) ? value : [value]).filter((item): item is string => Boolean(item?.trim())).map(item => item.trim()))];
        if (values.length) {
          stopped = true;
          if (options.onResults) options.onResults(values);
          else options.onResult(values[0]);
          return;
        }
      }
    } catch { /* Retry frames while the camera settles. */ }
    if (!stopped) timer = setTimeout(() => void scan(), 250);
  };
  void scan();
  return () => { stopped = true; if (timer !== undefined) clearTimeout(timer); if (nativeTimer !== undefined) clearTimeout(nativeTimer); };
}
