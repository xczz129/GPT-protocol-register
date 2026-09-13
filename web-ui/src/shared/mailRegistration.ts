/** Shared by both task forms so displayed values match the submitted account slots. */
export interface RegistrationNumbers {
  mailType: string;
  total: unknown;
  concurrency: unknown;
}

export function smsbowerPolicy(mailType: string, registerChild: unknown = true) {
  const selected = mailType === "smsbower_mail";
  const paired = selected && registerChild !== false;
  return {
    selected,
    paired,
    totalMin: paired ? 2 : 1,
    totalStep: paired ? 2 : 1,
    concurrencyMin: paired ? 2 : 1,
    concurrencyStep: paired ? 2 : 1,
  };
}

function boundedInteger(value: unknown, minimum: number, maximum: number): number {
  const number = Number(value);
  return Math.max(minimum, Math.min(maximum, Number.isFinite(number) ? Math.floor(number) : minimum));
}

export function normalizeRegistrationNumbers(form: RegistrationNumbers, registerChild: unknown = true) {
  const policy = smsbowerPolicy(form.mailType, registerChild);
  const total = boundedInteger(form.total, policy.totalMin, 500);
  const concurrency = boundedInteger(form.concurrency, policy.concurrencyMin, 64);
  return {
    total: policy.paired ? Math.ceil(total / 2) * 2 : total,
    concurrency: policy.paired ? Math.ceil(concurrency / 2) * 2 : concurrency,
  };
}

export function formatRegistrationRate(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "—" : `${(Math.max(0, Math.min(1, value)) * 100).toFixed(1)}%`;
}

export function formatOtpSeconds(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "—" : `${Math.max(0, value).toFixed(1)} 秒`;
}

export type MailRegistrationOutcome = "success" | "already_registered" | "otp_timeout" | "other_failed" | "cancelled";
export type MailRegistrationRole = "primary" | "child";

export interface MailRegistrationSummary {
  totals: Record<MailRegistrationOutcome | "attempted" | "in_progress", number>;
  rates: Record<MailRegistrationOutcome, { numerator: number; denominator: number; value: number | null }>;
  otp_wait: {
    received_count: number;
    timeout_count: number;
    average_seconds: number | null;
    min_seconds: number | null;
    max_seconds: number | null;
  };
}

export interface MailRegistrationStats extends MailRegistrationSummary {
  ok: boolean;
  error?: string;
  provider: string;
  by_domain: (MailRegistrationSummary & { domain: string })[];
  by_role: (MailRegistrationSummary & { role: MailRegistrationRole })[];
  groups: (MailRegistrationSummary & { provider: string; domain: string; role: MailRegistrationRole })[];
  updated_at: string | null;
}
