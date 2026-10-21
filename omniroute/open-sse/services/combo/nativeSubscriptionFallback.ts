/**
 * Native protocol subscription-first fallback eligibility.
 *
 * The caller supplies resolved provider-family and upstream-model identities; this
 * helper deliberately does not infer aliases or substitute a related model.
 */
export interface NativeFallbackRoute {
  providerFamily: string;
  upstreamModel: string;
  connectionId: string | null;
  billing: "subscription" | "api";
}

export interface NativeFallbackPolicy {
  subscriptionOnly: boolean;
  /** Undefined means unrestricted; an explicit empty list denies every connection. */
  allowedConnectionIds?: readonly string[];
}

/** Keep only same-family, exact-upstream-model API routes permitted by the key. */
export function selectNativePaidFallbacks<T extends NativeFallbackRoute>(
  subscription: Pick<NativeFallbackRoute, "providerFamily" | "upstreamModel">,
  candidates: readonly T[],
  policy: NativeFallbackPolicy
): T[] {
  if (policy.subscriptionOnly) return [];


  const allowed = policy.allowedConnectionIds;
  return candidates.filter(
    (candidate) =>
      candidate.billing === "api" &&
      candidate.providerFamily === subscription.providerFamily &&
      candidate.upstreamModel === subscription.upstreamModel &&
      candidate.connectionId !== null &&
      (allowed === undefined || allowed.includes(candidate.connectionId))
  );
}

/** Whether the upstream may have accepted a native Codex submission. */
export function isNativeSubmissionAmbiguous(code: unknown): boolean {
  return code === "chatgpt_submission_ambiguous";
}

const nativeTerminalResponses = new WeakSet<Response>();
const nativeTargetSignals = new WeakSet<AbortSignal>();

export function markNativeTargetSignal(signal: AbortSignal | null | undefined): void {
  if (signal) nativeTargetSignals.add(signal);
}

export function isNativeTargetSignal(signal: AbortSignal): boolean {
  return nativeTargetSignals.has(signal);
}

/** Trusted dispatch evidence; unlike a header, upstreams and clients cannot forge it. */
export function markNativeResponseTerminal(response: Response): Response {
  nativeTerminalResponses.add(response);
  return response;
}

export function isNativeResponseTerminal(response: Response): boolean {
  return nativeTerminalResponses.has(response);
}

export function inheritNativeResponsePolicy(source: Response, target: Response): Response {
  if (nativeTerminalResponses.has(source)) nativeTerminalResponses.add(target);
  return target;
}
