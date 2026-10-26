export interface PortalConfig {
  enabled: boolean;
  browser_login_enabled: boolean;
  mini_app_enabled: boolean;
  login_path: string | null;
  bot_url: string | null;
  support_text: string;
  support_url: string | null;
  notice: { title: string; body: string; starts_at: string; ends_at: string } | null;
  reminders_enabled: boolean;
}

export interface PortalMe {
  display_name: string;
  csrf_token: string;
}

export interface PortalPlan {
  id: number;
  name: string;
  description: string | null;
  duration_days: number | null;
  traffic_limit_gb: number | null;
  max_devices: number;
  price_amount: number;
  currency: string;
  is_trial: boolean;
}

export type PortalTrialState =
  | "disabled"
  | "available"
  | "capacity_paused"
  | "preparing"
  | "active"
  | "used";

export interface PortalTrial {
  state: PortalTrialState;
  duration_days: number;
  profile_limit: number;
  subscription_id: number | null;
  access_key_id: number | null;
  expires_at: string | null;
}

export interface PortalSubscription {
  id: number;
  service_name: string;
  state: string;
  starts_at: string | null;
  expires_at: string | null;
  profile_limit: number;
  profiles_used: number;
  traffic_limit_gb_per_profile: number | null;
}

export interface PortalProfile {
  id: number;
  subscription_id: number;
  display_name: string;
  state: string;
  can_connect: boolean;
}

export interface PortalConnection {
  uri: string;
}

export interface PortalDevice extends PortalProfile {
  legacy_profile_id: number | null;
  credential_generation: number;
  operation: PortalDeviceOperation | null;
}

export interface PortalDeviceOperation {
  id: string;
  request_id: string;
  device_id: number;
  action: "create" | "revoke" | "replace";
  stage: "revoking" | "provisioning" | "completed" | "failed" | "uncertain";
  credential_generation: number;
  endpoint_id: number | null;
  public_error: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

export type PortalUsageDays = 1 | 7 | 30;

export interface PortalUsageTotals {
  upload_bytes: number | null;
  download_bytes: number | null;
  measured_since: string | null;
  last_sample_at: string | null;
  coverage: "none" | "partial" | "current" | "stale" | "unavailable";
}

export interface PortalUsage extends PortalUsageTotals {
  days: PortalUsageDays;
  devices: Array<PortalUsageTotals & { device_id: number; display_name: string }>;
}

export interface PortalPreferences { expiry_reminders: boolean; }

export interface CreatePortalDevice {
  request_id: string;
  subscription_id: number;
  endpoint_id: number;
  display_name: string;
}

export interface PortalDeviceLocation {
  endpoint_id: number;
  name: string;
  country_code: string | null;
  is_published: boolean;
}

export interface PortalDeviceLocations {
  current: PortalDeviceLocation | null;
  pending_endpoint_id: number | null;
  selection_state: "ready" | "preparing" | "failed" | "verification_required" | "unavailable";
  available: PortalDeviceLocation[];
}

export interface PortalDeviceConnection {
  subscription_url: string;
  direct_uri: string;
}
