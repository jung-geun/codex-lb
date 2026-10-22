import type { CredentialLeaseSelectionContext } from "./exclusiveConnectionLeasePolicy";
import type { ConnectionRestrictionSource } from "./credentialSelectionDiagnostics.ts";

export interface CredentialSelectionOptions {
  /** Provenance of the effective allowlist; does not alter authorization. */
  connectionRestrictionSources?: readonly ConnectionRestrictionSource[];
  /** Exclude credential-specific transports that cannot serve caller tools. */
  requireToolCalling?: boolean;
  allowSuppressedConnections?: boolean;
  allowRateLimitedConnections?: boolean;
  bypassQuotaPolicy?: boolean;
  /** Trusted one-dispatch fallback: retain cached policies, but do not spend on live probes. */
  skipLiveQuotaPreflight?: boolean;
  /** Internal native billing admission; never sourced from a client body or header. */
  nativeBillingPreflight?: boolean;
  nativeContinuation?: boolean;
  /** Trusted server-resolved catalog metadata; never a client body/header field. */
  modelMetadata?: unknown;
  forcedConnectionId?: string | null;
  excludeConnectionIds?: string[] | null;
  sessionKey?: string | null;
  sessionAffinityTtlMs?: number | null;
  reserveOAuthSession?: boolean;
  lease?: CredentialLeaseSelectionContext;
  materializeCredentials?: boolean;
  deferLeaseClaim?: boolean;
  /** Internal: a same-call UNIQUE retry already holds the provider/owner selection lock. */
  _leaseRetryWithLockHeld?: boolean;
  /** Internal: freeze the original policy-valid candidate set across lease race/preflight retry. */
  _leaseCandidateIds?: string[];
  /** Antigravity account lease (#10011): only the final chat dispatch opts in. */
  reserveAntigravityLease?: boolean;
  routingRequestId?: string | null;
}
